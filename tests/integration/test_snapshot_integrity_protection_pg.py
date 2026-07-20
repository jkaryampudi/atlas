"""P0.1 (ADR-0018) snapshot protection: an authoritative NAV/attribution snapshot
is guarded at WRITE time (never created over a book that holds non-authoritative
lots) and re-verified at SERVE time (never served as authoritative if integrity
fails). Explicitly non-authoritative scopes (research_shadow / all_simulated)
keep working. No schema migration — integrity is enforced by the guard + an audit
stamp, not a stored per-row column."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from atlas.core.clock import FrozenClock
from atlas.dcp.portfolio.attribution import PortfolioIntegrityError
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.proposals import approve, build_proposal, settle_orders, snapshot
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
FX = Decimal("1.5")


def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals SET risk_check_id=NULL, "
                   "state='draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots",
              "reporting.attribution_daily"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies WHERE family='xsmom-pit-tr'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol='ZSNAP')"))
    # the api fixture COMMITS this book, so committed fx rows survive clean_audit's
    # rollback — delete the seed's own rows (source='snap') or a later fill-gate
    # test (which relies on there being NO 2026-07-14 rate) fills its own order.
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE base='USD' "
                   "AND quote='AUD' AND source='snap' "
                   "AND rate_date IN ('2026-07-10','2026-07-14')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol='ZSNAP'"))


def _seed_paper_book(s, clock) -> None:
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) VALUES "
        "('ZSNAP','XTEST','US','stock','ZSNAP','Information Technology','USD') "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid,:d,100,101,99,100,1000000,'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)} for i in range(21)])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-10',:r,'snap'),('USD','AUD','2026-07-14',:r,"
        "'snap') ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": FX})
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family,name,version,spec,code_sha,"
        " tolerance_bands,state) VALUES ('xsmom-pit-tr','x','1.0.0','{}','s','{}',"
        " 'paper') RETURNING id")).scalar()
    sig = s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid,:iid,'2026-07-13','long',1,0.5,'2026-08-31',:ca) "
        "RETURNING id"), {"sid": sid, "iid": iid, "ca": clock.now()}).scalar()
    memo = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation,"
        " evidence_refs, created_at) VALUES ('committee','ZSNAP','BUY','[]',:ca) "
        "RETURNING id"), {"ca": clock.now()}).scalar())
    res = build_proposal(s, clock, memo_id=memo, symbol="ZSNAP",
                         signal_refs=[str(sig)], entry_price=Decimal("100"),
                         stop_price=Decimal("95"), target_price=Decimal("120"))
    clock.advance_to(T0 + timedelta(hours=1))
    approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid,'2026-07-14',102,104,101,103,1000000,'EodhdAdapter')"),
        {"iid": iid})
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    settle_orders(s, clock)                          # the paper lot now exists
    clock.advance_to(datetime(2026, 7, 14, 23, 0, tzinfo=UTC))
    snapshot(s, clock)                               # a VERIFIED authoritative snapshot


def _downgrade(s) -> None:
    s.execute(text("UPDATE quant.strategies SET state='research_shadow' "
                   "WHERE family='xsmom-pit-tr'"))


def test_snapshot_write_fails_closed_when_integrity_fails(clean_audit):
    """Test 9: an authoritative snapshot cannot be CREATED when the book holds a
    non-authoritative lot."""
    s = clean_audit
    clock = FrozenClock(T0)
    _seed_paper_book(s, clock)
    _downgrade(s)
    clock.advance_to(datetime(2026, 7, 15, 23, 0, tzinfo=UTC))
    with pytest.raises(PortfolioIntegrityError):
        snapshot(s, clock)


def test_snapshot_write_stamps_integrity_verified(clean_audit):
    """The authoritative snapshot's audit event is stamped integrity_status."""
    s = clean_audit
    _seed_paper_book(s, FrozenClock(T0))
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type='portfolio.snapshot.created' "
        "ORDER BY seq DESC LIMIT 1")).scalar_one()
    assert payload["integrity_status"] == "VERIFIED"


@pytest.fixture
def api(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = clean_audit
    _seed_paper_book(s, FrozenClock(T0))
    _downgrade(s)                                    # book now holds a shadow lot
    s.commit()
    yield TestClient(app), s
    _clean(s)
    s.commit()
    reset_app_engine()


def test_precomputed_snapshot_with_shadow_activity_is_rejected(api):
    """Test 8: a precomputed authoritative snapshot exists (written while paper),
    but the strategy is now research_shadow — the serve path re-verifies and
    REFUSES to serve it as authoritative (not silently recalculated/repaired)."""
    client, _ = api
    r = client.get("/v1/portfolio/snapshot")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INTEGRITY_FAILED"
    # the default authoritative attribution view is likewise withheld
    d = client.get("/v1/portfolio/attribution/daily")
    assert d.status_code == 409
    assert d.json()["error"]["code"] == "INTEGRITY_FAILED"


def test_research_shadow_scope_still_serves_under_a_shadow_book(api):
    """Test 11: research-shadow reporting continues under its explicitly
    non-authoritative scope even though the book holds a shadow lot."""
    client, _ = api
    r = client.get("/v1/portfolio/attribution/daily?scope=research_shadow")
    assert r.status_code == 200
    assert r.json()["performance_scope"] == "research_shadow"
    assert r.json()["authoritative"] is False


def test_all_simulated_scope_is_explicitly_non_authoritative(api):
    """Test 12: all_simulated serves (explicit request) and is non-authoritative."""
    client, _ = api
    r = client.get("/v1/portfolio/attribution/daily?scope=all_simulated")
    assert r.status_code == 200
    assert r.json()["authoritative"] is False
