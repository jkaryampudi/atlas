"""WHAT-IF RISK PRE-FLIGHT over HTTP (POST /v1/risk/preflight): a strictly
read-only dry run of the proposal path — sizing, the itemised L1-L11 verdict,
and the uniform §3.3 error envelopes — with the zero-write property asserted
directly against row counts. A what-if is a question, not a material action:
no proposal row, no risk-check row, no audit event may appear.

Pinned numbers on the standard seeded book (A$100k cash, entry 100, stop 95,
USD/AUD 1.5, limit set v1): the §4 size is min(L6 133.33, L1 53.33, L10 50000)
-> qty 53 bound by L1/L2, and L1's post-trade weight is 53*150/100000 = 0.0795
vs the 0.08 cap.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from atlas.api.routers import risk as risk_router
from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)   # limit set v1 first effective day

# tables a pre-flight must never touch (the zero-write assertion)
_WRITE_TABLES = ("trading.trade_proposals", "risk.risk_checks",
                 "audit.decision_events", "trading.orders")


def _wipe(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE source = 'pf-test'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZPRE%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZPRE%'"))


def _seed_instrument(s, symbol: str, sector: str) -> str:
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, :sec, 'USD') RETURNING id"),
        {"sym": symbol, "sec": sector}).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        "volume, source) VALUES (:iid, :d, 100, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)} for i in range(21)])
    return str(iid)


@pytest.fixture
def pfclient(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    clock = FrozenClock(T0)
    monkeypatch.setattr(risk_router, "_clock", lambda: clock)

    s = clean_audit
    _wipe(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    _seed_instrument(s, "ZPRE1", "Information Technology")
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2026-07-10', 1.5, 'pf-test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = 1.5"))
    s.commit()

    yield TestClient(app), s, clock
    _wipe(s)
    s.commit()
    reset_app_engine()


def _counts(s) -> dict[str, int]:
    return {t: s.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in _WRITE_TABLES}


def _preflight(c, symbol="ZPRE1", entry="100", stop="95"):
    return c.post("/v1/risk/preflight", json={
        "symbol": symbol, "entry_price": entry, "stop_price": stop})


def _seed_correlated_book(s) -> None:
    """One open position (40 ZPRE2 @ close 100, stop 95 -> A$6,000 marked value,
    A$300 open risk) with only 21 bars of history: the ZPRE1/ZPRE2 pair has 20
    overlapping returns < the 60 minimum, so the L8 feed fails closed to corr 1
    > the 0.8 threshold. NAV = 100k cash + 6k position = 106k; the §4 size for
    ZPRE1 is 56 (L1 weight cap), so the combined weight is
    (6000 + 56*150)/106000 = 0.135849... > the 0.12 L8 combined cap -> L8 is
    the ONE failing rule (stop at 95 keeps L7 at 720/106000 = 0.0068)."""
    iid2 = _seed_instrument(s, "ZPRE2", "Health Care")
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at, current_stop) "
        "VALUES (:iid, 40, 100, 'USD', :at, 95)"),
        {"iid": iid2, "at": datetime(2026, 7, 10, 20, 0, tzinfo=UTC)})


def test_preflight_pass_pins_qty_and_l1(pfclient):
    c, s, _ = pfclient
    before = _counts(s)
    r = _preflight(c)
    assert r.status_code == 200
    d = r.json()
    assert d["verdict"] == "PASS"
    assert d["qty"] == 53                       # §4: L1 weight cap binds, not L6
    assert d["binding_constraint"] == "L1/L2"
    assert d["sizing_accepted"] is True
    assert d["breaker"] == "none"
    assert d["limit_set_version"] == 1
    assert d["nav_aud"] == pytest.approx(100000)
    assert "never a pre-commitment" in d["advisory"]

    rules = [x["rule"] for x in d["results"]]
    assert rules == ["DD", "L1", "L2", "L3", "L4", "L5", "L6",
                     "L7", "L8", "L9", "L10", "L11"]   # itemised, no short-circuit
    l1 = next(x for x in d["results"] if x["rule"] == "L1")
    assert l1["pass"] is True
    assert l1["value"] == pytest.approx(0.0795)  # 53 * 100 * 1.5 / 100000
    assert l1["limit"] == 0.08
    assert all(x["pass"] for x in d["results"])

    assert _counts(s) == before                 # a question leaves no trace


def test_preflight_fail_pins_l8_on_correlated_book(pfclient):
    c, s, _ = pfclient
    _seed_correlated_book(s)
    s.commit()
    before = _counts(s)

    r = _preflight(c)
    assert r.status_code == 200                 # a FAIL verdict is an answer, not an error
    d = r.json()
    assert d["verdict"] == "FAIL"
    assert d["qty"] == 56                       # sizing accepted; validate failed
    assert d["sizing_accepted"] is True
    failures = [x for x in d["results"] if not x["pass"]]
    assert [x["rule"] for x in failures] == ["L8"]
    l8 = failures[0]
    assert l8["value"] == pytest.approx(14400 / 106000)   # combined weight 0.135849…
    assert l8["limit"] == 0.12
    assert "ZPRE2" in l8["detail"]              # the FAIL explains itself completely

    assert _counts(s) == before


def test_preflight_zero_writes_across_all_outcomes(pfclient):
    """The deliverable assertion: PASS, sizing-FAIL, 400, and 404 all leave
    trade_proposals / risk_checks / audit.decision_events / orders untouched."""
    c, s, _ = pfclient
    before = _counts(s)
    assert _preflight(c).status_code == 200                          # PASS
    assert _preflight(c, entry="1000", stop="500").json()["verdict"] == "FAIL"
    assert _preflight(c, symbol="ZNOPE").status_code == 404          # unknown
    assert _preflight(c, entry="0", stop="-1").status_code == 400    # malformed
    assert _counts(s) == before


def test_preflight_sizing_rejection_is_itemised_sizing_fail(pfclient):
    c, _, _ = pfclient
    # entry 1000, stop 500: L6 risk budget (A$1,000 / A$750-per-share) sizes to
    # qty 1, but value 1*1000*1.5 = A$1,500 < the A$2,000 §4 minimum -> the §4
    # rejection reports as the single itemised rule 'SIZING', L1-L11 unevaluated
    r = _preflight(c, entry="1000", stop="500")
    d = r.json()
    assert r.status_code == 200
    assert d["verdict"] == "FAIL"
    assert d["sizing_accepted"] is False
    assert d["qty"] == 0
    assert d["binding_constraint"] == "min_position"
    assert [x["rule"] for x in d["results"]] == ["SIZING"]
    sizing = d["results"][0]
    assert sizing["pass"] is False and sizing["value"] is None
    assert "1500.00 AUD" in sizing["detail"]
    assert "(binding: min_position)" in sizing["detail"]


def test_preflight_envelopes_400_404_409(pfclient, monkeypatch):
    c, _, clock = pfclient

    r = _preflight(c, entry="-5")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PRICES"
    r = _preflight(c, entry="100", stop="0")            # stop <= 0 is malformed
    assert r.status_code == 400
    r = _preflight(c, entry="95", stop="100")           # stop >= entry
    assert r.status_code == 400
    assert "long-only" in r.json()["error"]["message"]

    r = _preflight(c, symbol="ZNOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"

    # before the limit set's effective date the engine honestly refuses to run
    early = FrozenClock(datetime(2026, 7, 12, 20, 0, tzinfo=UTC))
    monkeypatch.setattr(risk_router, "_clock", lambda: early)
    r = _preflight(c)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "NO_ACTIVE_LIMIT_SET"
    assert "no limit set effective" in err["message"]
