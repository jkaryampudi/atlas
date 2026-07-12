"""Monthly attribution (Doc 04 §14 standing line; Doc 06 §2 resource map).

Reuses the exits-suite seeding verbatim (test_exits_pg golden numbers: entry
fill 102.102, stop exit at 94.905) and pins the FULL 2026-07 attribution to
the cent / basis point, hand-computed:

  entry (buy 53 @ decision 100, filled 102 open + 10bps = 102.102):
    shortfall_bps = (102.102-100)/100 x 1e4          = 210.2000
    cost_aud      = 210.2/1e4 x 100 x 53 x 1.5       = 167.109  -> 167.11
    lot cost_aud  = 53 x 102.102 x 1.5               = 8117.109 -> 8117.11
  exit (stop 95 hit, filled 95 - 10bps = 94.905):
    shortfall_bps = -(94.905-95)/95 x 1e4            = 10.0000
    cost_aud      = 10/1e4 x 95 x 53 x 1.5           = 7.5525   -> 7.55
    lot proceeds  = 53 x 94.905 x 1.5                = 7544.9475 -> 7544.95
  realised P&L    = 7544.95 - 8117.11                = -572.16 (1 lot closed)
  NAV snapshots (boundary convention: earliest-inside -> latest-inside):
    S1 2026-07-14 23:00 = (100000 - 8117.109) + 53x103x1.5 = 100071.39
    S2 2026-07-15 23:00 = 91882.891 + 7544.9475            = 99427.84
    swing = 99427.84 - 100071.39                           = -643.55
  llm_spend_usd   = 0.1234 + 2.0000 (July runs)            = 2.1234
                    (the 9.9999 June run is OUTSIDE the period)

Compute-plane tests roll back (pg_session); the API tests commit the same
round trip (the app reads through its own engine) and wipe on teardown,
exactly like test_api_trading_pg.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from atlas.core.clock import FrozenClock
from atlas.dcp.portfolio.attribution import Attribution, ShortfallLine, compute_attribution
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.exits import scan_stop_exits
from atlas.dcp.trading.proposals import approve, build_proposal, settle_orders, snapshot
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
# Monday 2026-07-13: the first day limit set v1 is effective (exits-suite T0)
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
NEXT_SESSION = date(2026, 7, 14)   # entry fill day (XNYS session after T0)
STOP_SESSION = date(2026, 7, 15)   # stop-exit fill day
FX_USD_AUD = Decimal("1.5")
FX_SOURCE = "attr-test"            # own source string so teardown deletes cleanly


# ------------------------------------------------------------------- seeding

def _clean(s) -> None:
    """Remove committed debris from this suite (and crashed runs), FK-safe."""
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE source = :src"),
              {"src": FX_SOURCE})
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZATR%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZATR%'"))
    s.execute(text("DELETE FROM research.agent_runs"))
    s.execute(text("DELETE FROM research.memos"))


def _fx(s, *, day: date) -> None:
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', :d, :r, :src) "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"d": day, "r": FX_USD_AUD, "src": FX_SOURCE})


def _agent_run(s, *, at: datetime, cost: Decimal) -> None:
    s.execute(text(
        "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, model, "
        " status, cost_usd, created_at) "
        "VALUES ('research_analyst', 'attr-test', 'none', 'ok', :c, :at)"),
        {"c": cost, "at": at})


def _round_trip(s) -> None:
    """The exits-suite happy path, verbatim golden numbers: ZATR 53 @ 102.102
    entry fill (2026-07-14), snapshot, stop exit at 94.905 (2026-07-15),
    snapshot; plus three agent runs (two in July, one in June)."""
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) VALUES "
        "('ZATR', 'XTEST', 'US', 'stock', 'ZATR', 'Information Technology', 'USD') "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, close, volume, source) "
        "VALUES (:iid, :d, 100, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)} for i in range(21)])
    _fx(s, day=date(2026, 7, 10))
    memo_id = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, recommendation, evidence_refs) "
        "VALUES ('committee', 'BUY', '[]') RETURNING id")).scalar())

    clock = FrozenClock(T0)
    res = build_proposal(
        s, clock, memo_id=memo_id, symbol="ZATR", signal_refs=[str(uuid4())],
        entry_price=Decimal("100"), stop_price=Decimal("95"),
        target_price=Decimal("120"))
    assert res.state == "pending_approval"
    clock.advance_to(T0 + timedelta(hours=1))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        "volume, source) VALUES (:iid, :d, 102, 103, 1000000, 'EodhdAdapter')"),
        {"iid": iid, "d": NEXT_SESSION})
    _fx(s, day=NEXT_SESSION)
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    fills = settle_orders(s, clock)
    assert len(fills) == 1 and fills[0].fill_price == Decimal("102.102000")

    clock.advance_to(datetime(2026, 7, 14, 23, 0, tzinfo=UTC))
    s1 = snapshot(s, clock)
    assert s1.nav_aud == Decimal("100071.39")   # 91882.891 + 53*103*1.5

    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, low, "
        "close, volume, source) VALUES (:iid, :d, 96, 94, 94.5, 1000000, 'EodhdAdapter')"),
        {"iid": iid, "d": STOP_SESSION})
    _fx(s, day=STOP_SESSION)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))
    reports = scan_stop_exits(s, clock)
    assert len(reports) == 1 and reports[0].fill_price == Decimal("94.905000")

    clock.advance_to(datetime(2026, 7, 15, 23, 0, tzinfo=UTC))
    s2 = snapshot(s, clock)
    assert s2.nav_aud == Decimal("99427.84")    # exits-suite pinned cash ledger

    _agent_run(s, at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC), cost=Decimal("0.1234"))
    _agent_run(s, at=datetime(2026, 7, 31, 23, 59, tzinfo=UTC), cost=Decimal("2.0000"))
    _agent_run(s, at=datetime(2026, 6, 30, 23, 59, tzinfo=UTC), cost=Decimal("9.9999"))


# ------------------------------------------------------------- compute plane

def test_full_month_attribution_pinned(clean_audit):
    """The 2026-07 attribution, every figure hand-computed (module docstring)."""
    s = clean_audit
    _round_trip(s)

    attr = compute_attribution(s, year=2026, month=7)
    assert attr == Attribution(
        period="2026-07", trades_buy=1, trades_sell=1,
        entry_shortfall=ShortfallLine(fills=1, qty=53, avg_bps=Decimal("210.2000"),
                                      cost_aud=Decimal("167.11")),
        exit_shortfall=ShortfallLine(fills=1, qty=53, avg_bps=Decimal("10.0000"),
                                     cost_aud=Decimal("7.55")),
        realised_pnl_aud=Decimal("-572.16"), lots_closed=1,
        nav_start_aud=Decimal("100071.39"), nav_end_aud=Decimal("99427.84"),
        unrealised_swing_aud=Decimal("-643.55"),
        llm_spend_usd=Decimal("2.1234"))
    # quantization is part of the pin, not just numeric equality
    assert str(attr.entry_shortfall.avg_bps) == "210.2000"
    assert str(attr.exit_shortfall.cost_aud) == "7.55"
    assert str(attr.realised_pnl_aud) == "-572.16"


def test_adjacent_months_respect_period_bounds(clean_audit):
    """June sees ONLY its agent run (9.9999); August sees nothing — July's
    snapshots may not leak into an August swing (no snapshot INSIDE August,
    so all three NAV fields stay None even though earlier snapshots exist)."""
    s = clean_audit
    _round_trip(s)

    june = compute_attribution(s, year=2026, month=6)
    assert june.llm_spend_usd == Decimal("9.9999")
    assert (june.trades_buy, june.trades_sell, june.lots_closed) == (0, 0, 0)
    assert june.realised_pnl_aud == Decimal("0.00")
    assert june.entry_shortfall == ShortfallLine(0, 0, None, Decimal("0.00"))
    assert june.nav_start_aud is None and june.unrealised_swing_aud is None

    aug = compute_attribution(s, year=2026, month=8)
    assert aug.llm_spend_usd == Decimal("0.0000")
    assert (aug.nav_start_aud, aug.nav_end_aud, aug.unrealised_swing_aud) \
        == (None, None, None)


def test_empty_month_returns_zeros_and_nones(clean_audit):
    """Empty book, empty month: the exact zero/None shape, pinned."""
    s = clean_audit
    _clean(s)
    attr = compute_attribution(s, year=2026, month=5)
    assert attr == Attribution(
        period="2026-05", trades_buy=0, trades_sell=0,
        entry_shortfall=ShortfallLine(fills=0, qty=0, avg_bps=None,
                                      cost_aud=Decimal("0.00")),
        exit_shortfall=ShortfallLine(fills=0, qty=0, avg_bps=None,
                                     cost_aud=Decimal("0.00")),
        realised_pnl_aud=Decimal("0.00"), lots_closed=0,
        nav_start_aud=None, nav_end_aud=None, unrealised_swing_aud=None,
        llm_spend_usd=Decimal("0.0000"))
    # December wraps the year boundary without error
    assert compute_attribution(s, year=2026, month=12).period == "2026-12"
    with pytest.raises(ValueError):
        compute_attribution(s, year=2026, month=0)
    with pytest.raises(ValueError):
        compute_attribution(s, year=2026, month=13)


# ---------------------------------------------------------------- API surface

@pytest.fixture
def aclient(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = clean_audit
    _round_trip(s)
    s.commit()
    yield TestClient(app), s
    _clean(s)
    s.commit()
    reset_app_engine()


def test_api_attribution_full_month_exact_strings(aclient):
    """House convention: exact quantized figures as strings, no float drift.
    Whole-body equality pins the JSON shape."""
    c, _ = aclient
    r = c.get("/v1/portfolio/attribution/2026-07")
    assert r.status_code == 200
    assert r.json() == {
        "period": "2026-07", "trades_buy": 1, "trades_sell": 1,
        "entry_shortfall": {"fills": 1, "qty": 53,
                            "avg_bps": "210.2000", "cost_aud": "167.11"},
        "exit_shortfall": {"fills": 1, "qty": 53,
                           "avg_bps": "10.0000", "cost_aud": "7.55"},
        "realised_pnl_aud": "-572.16", "lots_closed": 1,
        "nav_start_aud": "100071.39", "nav_end_aud": "99427.84",
        "unrealised_swing_aud": "-643.55", "llm_spend_usd": "2.1234"}


def test_api_attribution_empty_month_shape(aclient):
    c, _ = aclient
    r = c.get("/v1/portfolio/attribution/2026-01")
    assert r.status_code == 200
    assert r.json() == {
        "period": "2026-01", "trades_buy": 0, "trades_sell": 0,
        "entry_shortfall": {"fills": 0, "qty": 0, "avg_bps": None,
                            "cost_aud": "0.00"},
        "exit_shortfall": {"fills": 0, "qty": 0, "avg_bps": None,
                           "cost_aud": "0.00"},
        "realised_pnl_aud": "0.00", "lots_closed": 0,
        "nav_start_aud": None, "nav_end_aud": None,
        "unrealised_swing_aud": None, "llm_spend_usd": "0.0000"}


def test_api_attribution_malformed_period_envelope(aclient):
    """Doc 06 §3.3 uniform envelope on every malformed period — regex misses
    AND regex-passing impossibilities (month 00/13, year 0000)."""
    c, _ = aclient
    for bad in ("garbage", "2026-7", "20260-07", "2026-07-01",
                "2026-13", "2026-00", "0000-01"):
        r = c.get(f"/v1/portfolio/attribution/{bad}")
        assert r.status_code == 400, bad
        err = r.json()["error"]
        assert err["code"] == "INVALID_PERIOD"
        assert err["message"] and err["details"] is None
