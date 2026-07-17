"""Daily core/satellite attribution (atlas/dcp/reporting/attribution.py,
migration 0027, ADR-0012 consequence 4) — hand-derived golden three-day book.

THE BOOK (all USD, FX 1.5 every day; satellite entered via the REAL lifecycle
build -> approve -> settle so the bands.py join is exercised end to end):

  pre-series  core: 100 ZATC @ 10 held is_core (in-kind, pre-inception lot,
              cost A$1500); ledger cash A$100,000; xsmom strategy signed with
              one ZATS signal; entry proposal built 7-13 (entry 100, stop 95)
              sizes to 54 shares (L1 8% weight cap: floor(101500*0.08/150))
  d1 = 7-13   snapshot: NAV = 100000 + 100*10*1.5          = 101500.00
  d2 = 7-14   ZATS fills at open 102 + 10bps = 102.102 (the mid-window fill);
              lot cost = 54*102.102*1.5 = 8270.262 -> booked 8270.26
              closes: ZATC 11, ZATS 103, SPY 505, INDA 51.5
              NAV = (100000 - 8270.262) + 1650 + 8343      = 101722.74
  d3 = 7-15   variant A (hold): ZATC 10, ZATS 105, SPY 500, INDA 51.5
              NAV = 91729.738 + 1500 + 8505                = 101734.74
              variant B (stop): low 94 fires the 95 stop, fill 94.905,
              proceeds = 54*94.905*1.5 = 7687.305 -> booked 7687.30
              NAV = 91729.738 + 7687.305 + 1500            = 100917.04

HAND-DERIVED RETURNS (the module-docstring convention, 8dp half-even;
ret = (V - P - F_in + F_out) / (P + F_in)):
  d2 core  = (1650-1500)/1500                              = 0.10000000
  d2 xsmom = (8343 - 0 - 8270.26)/(0 + 8270.26) = 72.74/8270.26
                                                           = 0.00879537
  d2 total = 101722.74/101500 - 1 = 222.74/101500          = 0.00219448
  d3A core = (1500-1650)/1650                              = -0.09090909
  d3A xsmom= (8505-8343)/8343 = 162/8343                   = 0.01941748
  d3A total= 12/101722.74                                  = 0.00011797
  d3B xsmom= (0 - 8343 + 7687.30)/8343 = -655.70/8343      = -0.07859283
  d3B total= -805.70/101722.74                             = -0.00792055

BENCHMARKS (TR = raw closes, no dividends seeded):
  SPY d2 = 505/500-1 = 0.01000000    SPY d3 = 500/505-1 = -0.00990099
  blend d2 = (0.55*0.01 + 0.15*0.03)/0.70 = 0.01/0.70      = 0.01428571
  blend d3 = (0.55*(-0.00990099...) + 0.15*0)/0.70         = -0.00777935

CONTRIBUTIONS (exact, C = dV - net flow; cash = dCash + net flows):
  d2: core +150.00, xsmom +72.74, pead 0, cash 0.00 -> sum 222.74 = dNAV
  d3A: core -150.00, xsmom +162.00, cash 0.00        -> sum  12.00 = dNAV
  d3B: core -150.00, xsmom -655.70, cash 0.00        -> sum -805.70 = dNAV

ALPHA (compounded satellite minus compounded SPY TR, x100, 2dp):
  A: (1.00879537*1.01941748 - 1.01*0.99009901)*100          = +2.84 pp
  B: (1.00879537*0.92140717 - 1.01*0.99009901)*100          = -7.05 pp
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from atlas.core.clock import FrozenClock
from atlas.dcp.reporting.attribution import (
    MONTHLY_REPORT_EVENT,
    MonthlyAttribution,
    SleeveMonth,
    backfill_attribution,
    compute_attribution_day,
    compute_monthly,
    generate_monthly_report,
)
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.exits import scan_stop_exits
from atlas.dcp.trading.proposals import approve, build_proposal, settle_orders, snapshot
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
SNAP1 = datetime(2026, 7, 13, 23, 0, tzinfo=UTC)
SETTLE = datetime(2026, 7, 14, 22, 0, tzinfo=UTC)
SNAP2 = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
STOP = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
SNAP3 = datetime(2026, 7, 15, 23, 0, tzinfo=UTC)
D1, D2, D3 = date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)
FX = Decimal("1.5")
FX_SOURCE = "attr-daily-test"


# ------------------------------------------------------------------- seeding

def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("reporting.attribution_daily", "trading.tax_lots",
              "trading.executions", "trading.orders", "trading.approvals",
              "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM quant.sleeve_daily"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies "
                   "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZAT%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZAT%'"))
    # benchmark debris in THIS suite's window only (other suites own theirs)
    s.execute(text(
        "DELETE FROM market.price_bars_daily WHERE bar_date BETWEEN :a AND :b "
        "AND instrument_id IN (SELECT id FROM market.instruments "
        "                      WHERE symbol IN ('SPY','INDA'))"),
        {"a": D1, "b": D3})
    # benchmark rows THIS suite created (exchange XTEST, only when the
    # canonical symbol was absent): a committed second SPY/INDA row would
    # pollute every suite that assumes one instrument per symbol
    s.execute(text(
        "DELETE FROM market.price_bars_daily WHERE instrument_id IN "
        "(SELECT id FROM market.instruments "
        " WHERE symbol IN ('SPY','INDA') AND exchange = 'XTEST')"))
    s.execute(text(
        "DELETE FROM market.corporate_actions WHERE instrument_id IN "
        "(SELECT id FROM market.instruments "
        " WHERE symbol IN ('SPY','INDA') AND exchange = 'XTEST')"))
    s.execute(text(
        "DELETE FROM market.instruments "
        "WHERE symbol IN ('SPY','INDA') AND exchange = 'XTEST'"))
    s.execute(text(
        "DELETE FROM market.corporate_actions "
        "WHERE action_date BETWEEN :a AND :b AND instrument_id IN "
        "(SELECT id FROM market.instruments WHERE symbol IN ('SPY','INDA'))"),
        {"a": D1, "b": D3})
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE source = :src"),
              {"src": FX_SOURCE})
    s.execute(text("DELETE FROM research.agent_runs"))
    s.execute(text("DELETE FROM research.memos"))


def _instrument(s, symbol: str, *, itype: str, sector: str) -> str:
    iid = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = :sym LIMIT 1"),
        {"sym": symbol}).scalar()
    if iid is None:
        iid = s.execute(text(
            "INSERT INTO market.instruments (symbol, exchange, market, "
            "instrument_type, name, sector_gics, currency) "
            "VALUES (:sym, 'XTEST', 'US', :t, :sym, :sec, 'USD') RETURNING id"),
            {"sym": symbol, "t": itype, "sec": sector}).scalar()
    return str(iid)


def _bar(s, iid: str, d: date, *, o, h, lo, c, v: int = 1_000_000) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :o, :h, :l, :c, :v, 'EodhdAdapter') "
        "ON CONFLICT (instrument_id, bar_date) DO UPDATE SET "
        "open = :o, high = :h, low = :l, close = :c, volume = :v, "
        "source = 'EodhdAdapter'"),
        {"iid": iid, "d": d, "o": o, "h": h, "l": lo, "c": c, "v": v})


def _seed_book(s, clock) -> dict[str, str]:
    """The docstring book up to the approved (unfilled) satellite entry."""
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    core_iid = _instrument(s, "ZATC", itype="etf", sector="Broad")
    sat_iid = _instrument(s, "ZATS", itype="stock",
                          sector="Information Technology")
    spy_iid = _instrument(s, "SPY", itype="etf", sector="Broad")
    inda_iid = _instrument(s, "INDA", itype="etf", sector="Broad")
    for i in range(21):                      # ADV window + entry marks
        _bar(s, sat_iid, date(2026, 6, 23) + timedelta(days=i),
             o=100, h=101, lo=99, c=100)
    _bar(s, core_iid, D1, o=10, h=10, lo=10, c=10)
    _bar(s, spy_iid, D1, o=500, h=500, lo=500, c=500)
    _bar(s, inda_iid, D1, o=50, h=50, lo=50, c=50)
    for d in (date(2026, 7, 10), D1, D2, D3):
        s.execute(text(
            "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, "
            "source) VALUES ('USD','AUD',:d,:r,:src) "
            "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
            {"d": d, "r": FX, "src": FX_SOURCE})

    # the passive core: an in-kind pre-inception holding (is_core, no stop) —
    # 100 ZATC @ 10, cost A$1500, no execution (ledger cash stays A$100k)
    pos_id = s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        " opened_at, current_stop, is_core, created_at) "
        "VALUES (:iid, 100, 10, 'USD', :at, NULL, true, :at) RETURNING id"),
        {"iid": core_iid, "at": datetime(2026, 6, 30, 12, 0, tzinfo=UTC)}).scalar()
    s.execute(text(
        "INSERT INTO trading.tax_lots (position_id, execution_id, qty, cost_aud, "
        " acquired_at, created_at) VALUES (:p, NULL, 100, 1500.00, :at, :at)"),
        {"p": pos_id, "at": datetime(2026, 6, 30, 12, 0, tzinfo=UTC)})

    strategy_id = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state) VALUES ('xsmom-pit-tr', 'xsmom_pit', '1.0.0', "
        "'{}', 'attr-test-sha', '{}', 'paper') RETURNING id")).scalar()
    signal_id = s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid, :iid, :d, 'long', 1, 0.5, '2026-08-31', :ca) "
        "RETURNING id"),
        {"sid": strategy_id, "iid": sat_iid, "d": D1, "ca": clock.now()}).scalar()
    memo_id = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee', 'ZATS', 'BUY', '[]', :ca) RETURNING id"),
        {"ca": clock.now()}).scalar())
    res = build_proposal(s, clock, memo_id=memo_id, symbol="ZATS",
                         signal_refs=[str(signal_id)],
                         entry_price=Decimal("100"), stop_price=Decimal("95"),
                         target_price=Decimal("120"))
    # L1 8% weight cap on NAV 101500: floor(101500*0.08/(100*1.5)) = 54
    assert res.state == "pending_approval" and res.qty == 54, res
    clock.advance_to(T0 + timedelta(hours=1))
    assert approve(s, clock, proposal_id=res.proposal_id,
                   acknowledged_risks=True).status == "approved"
    return {"core": core_iid, "sat": sat_iid, "spy": spy_iid,
            "inda": inda_iid}


def _day1(s, clock):
    clock.advance_to(SNAP1)
    assert snapshot(s, clock).nav_aud == Decimal("101500.00")
    return compute_attribution_day(s, clock)


def _day2(s, clock, ids):
    _bar(s, ids["sat"], D2, o=102, h=104, lo=101, c=103)
    _bar(s, ids["core"], D2, o=11, h=11, lo=11, c=11)
    _bar(s, ids["spy"], D2, o=505, h=505, lo=505, c=505)
    _bar(s, ids["inda"], D2, o=51.5, h=51.5, lo=51.5, c=51.5)
    clock.advance_to(SETTLE)
    fills = settle_orders(s, clock)
    assert len(fills) == 1 and fills[0].fill_price == Decimal("102.102000")
    clock.advance_to(SNAP2)
    assert snapshot(s, clock).nav_aud == Decimal("101722.74")
    return compute_attribution_day(s, clock)


def _day3_hold(s, clock, ids):
    _bar(s, ids["sat"], D3, o=105, h=106, lo=104, c=105)   # low 104: no stop
    _bar(s, ids["core"], D3, o=10, h=10, lo=10, c=10)
    _bar(s, ids["spy"], D3, o=500, h=500, lo=500, c=500)
    _bar(s, ids["inda"], D3, o=51.5, h=51.5, lo=51.5, c=51.5)
    clock.advance_to(SNAP3)
    assert snapshot(s, clock).nav_aud == Decimal("101734.74")
    return compute_attribution_day(s, clock)


def _day3_stopped(s, clock, ids):
    _bar(s, ids["sat"], D3, o=96, h=97, lo=94, c=94.5)     # 95 stop fires
    _bar(s, ids["core"], D3, o=10, h=10, lo=10, c=10)
    _bar(s, ids["spy"], D3, o=500, h=500, lo=500, c=500)
    _bar(s, ids["inda"], D3, o=51.5, h=51.5, lo=51.5, c=51.5)
    clock.advance_to(STOP)
    fired = scan_stop_exits(s, clock)
    assert len(fired) == 1 and fired[0].fill_price == Decimal("94.905000")
    clock.advance_to(SNAP3)
    assert snapshot(s, clock).nav_aud == Decimal("100917.04")
    return compute_attribution_day(s, clock)


def _rows(s) -> dict[tuple[date, str], tuple]:
    return {(r.session_date, r.sleeve):
            (Decimal(r.value_aud),
             Decimal(r.ret_1d) if r.ret_1d is not None else None,
             Decimal(r.benchmark_ret_1d)
             if r.benchmark_ret_1d is not None else None)
            for r in s.execute(text(
                "SELECT session_date, sleeve, value_aud, ret_1d, "
                "benchmark_ret_1d FROM reporting.attribution_daily"))}


# ------------------------------------------------------------- compute plane

# the docstring goldens, verbatim (value, ret_1d, benchmark_ret_1d)
GOLDEN_A = {
    (D1, "core"): (Decimal("1500.00"), None, None),
    (D1, "xsmom"): (Decimal("0.00"), None, None),
    (D1, "pead"): (Decimal("0.00"), None, None),
    (D1, "cash"): (Decimal("100000.00"), None, None),
    (D1, "total"): (Decimal("101500.00"), None, None),
    (D2, "core"): (Decimal("1650.00"), Decimal("0.10000000"),
                   Decimal("0.01428571")),
    (D2, "xsmom"): (Decimal("8343.00"), Decimal("0.00879537"),
                    Decimal("0.01000000")),
    (D2, "pead"): (Decimal("0.00"), None, Decimal("0.01000000")),
    (D2, "cash"): (Decimal("91729.74"), Decimal("0"), Decimal("0")),
    (D2, "total"): (Decimal("101722.74"), Decimal("0.00219448"),
                    Decimal("0.01000000")),
    (D3, "core"): (Decimal("1500.00"), Decimal("-0.09090909"),
                   Decimal("-0.00777935")),
    (D3, "xsmom"): (Decimal("8505.00"), Decimal("0.01941748"),
                    Decimal("-0.00990099")),
    (D3, "pead"): (Decimal("0.00"), None, Decimal("-0.00990099")),
    (D3, "cash"): (Decimal("91729.74"), Decimal("0"), Decimal("0")),
    (D3, "total"): (Decimal("101734.74"), Decimal("0.00011797"),
                    Decimal("-0.00990099")),
}


def test_three_day_book_every_row_hand_pinned(clean_audit):
    s = clean_audit
    clock = FrozenClock(T0)
    ids = _seed_book(s, clock)
    _day1(s, clock)
    _day2(s, clock, ids)
    r3 = _day3_hold(s, clock, ids)

    assert _rows(s) == GOLDEN_A
    # the t9 line, exactly as the operator reads it
    assert r3.summary() == ("attribution: core -9.09% vs blend -0.78% "
                            "· satellite +1.94% vs SPY -0.99% "
                            "· alpha +2.84pp cumulative")
    assert r3.alpha_pp == Decimal("2.84")
    assert r3.satellite_ret_1d == Decimal("0.01941748")


def test_flow_adjustment_books_the_buy_as_flow_not_return(clean_audit):
    """The mid-window fill: a naive diff would call d2 xsmom +inf% (0 -> 8343).
    The convention books cost 8270.26 as a flow and grades only the 72.74
    post-fill move: 72.74/8270.26 = 0.00879537."""
    s = clean_audit
    clock = FrozenClock(T0)
    ids = _seed_book(s, clock)
    _day1(s, clock)
    r2 = _day2(s, clock, ids)
    x = r2.by_sleeve()["xsmom"]
    assert x.flow_in_aud == Decimal("8270.26")     # the booked lot cost
    assert x.flow_out_aud == Decimal("0")
    assert x.ret_1d == Decimal("0.00879537")
    assert x.contribution_aud == Decimal("72.74")  # dV - flow, exact


def test_contributions_sum_to_nav_change_exactly(clean_audit):
    """The identity, in cents: sleeve contributions sum to the NAV change on
    EVERY day (cash is the exact residual; fees would surface there)."""
    s = clean_audit
    clock = FrozenClock(T0)
    ids = _seed_book(s, clock)
    _day1(s, clock)
    for report, d_nav in ((_day2(s, clock, ids), Decimal("222.74")),
                          (_day3_hold(s, clock, ids), Decimal("12.00"))):
        by = report.by_sleeve()
        parts = [by[k].contribution_aud for k in ("core", "xsmom", "pead", "cash")]
        assert all(p is not None for p in parts)
        assert sum(parts) == d_nav == by["total"].contribution_aud
    # and the hand values themselves
    assert by["core"].contribution_aud == Decimal("-150.00")
    assert by["xsmom"].contribution_aud == Decimal("162.00")
    assert by["cash"].contribution_aud == Decimal("0.00")


def test_stop_exit_outflow_convention(clean_audit):
    """Variant B: the sale's proceeds (7687.30) credit the numerator, its
    capital leaves the base — ret = -655.70/8343 = -0.07859283; the emptied
    sleeve stores A$0 (a real value); the identity still closes in cents."""
    s = clean_audit
    clock = FrozenClock(T0)
    ids = _seed_book(s, clock)
    _day1(s, clock)
    _day2(s, clock, ids)
    r3 = _day3_stopped(s, clock, ids)
    by = r3.by_sleeve()
    assert (by["xsmom"].value_aud, by["xsmom"].ret_1d) \
        == (Decimal("0.00"), Decimal("-0.07859283"))
    assert by["xsmom"].flow_out_aud == Decimal("7687.30")
    assert by["xsmom"].contribution_aud == Decimal("-655.70")
    assert by["cash"].value_aud == Decimal("99417.04")
    parts = [by[k].contribution_aud for k in ("core", "xsmom", "pead", "cash")]
    assert sum(parts) == Decimal("-805.70") == by["total"].contribution_aud
    assert by["total"].ret_1d == Decimal("-0.00792055")
    assert r3.alpha_pp == Decimal("-7.05")


def test_rerun_upserts_identically_including_created_at(clean_audit):
    s = clean_audit
    clock = FrozenClock(T0)
    ids = _seed_book(s, clock)
    _day1(s, clock)
    _day2(s, clock, ids)
    _day3_hold(s, clock, ids)
    before = s.execute(text(
        "SELECT session_date, sleeve, value_aud, ret_1d, benchmark_ret_1d, "
        "created_at FROM reporting.attribution_daily "
        "ORDER BY session_date, sleeve")).all()
    clock.advance_to(SNAP3 + timedelta(hours=2))
    again = compute_attribution_day(s, clock)
    assert again is not None and again.session == D3
    after = s.execute(text(
        "SELECT session_date, sleeve, value_aud, ret_1d, benchmark_ret_1d, "
        "created_at FROM reporting.attribution_daily "
        "ORDER BY session_date, sleeve")).all()
    assert after == before          # byte-identical, created_at untouched


def test_backfill_walks_the_same_code_path(clean_audit):
    """TRUNCATE the series, --backfill from the earliest snapshot: the same
    15 rows land (the daily node and the backfill share one code path)."""
    s = clean_audit
    clock = FrozenClock(T0)
    ids = _seed_book(s, clock)
    _day1(s, clock)
    _day2(s, clock, ids)
    _day3_hold(s, clock, ids)
    expected = _rows(s)
    s.execute(text("DELETE FROM reporting.attribution_daily"))
    reports = backfill_attribution(s, FrozenClock(SNAP3 + timedelta(hours=1)))
    assert [r.session for r in reports] == [D1, D2, D3]
    assert _rows(s) == expected == GOLDEN_A


def test_monthly_report_golden_and_audit_event(clean_audit, tmp_path):
    s = clean_audit
    clock = FrozenClock(T0)
    ids = _seed_book(s, clock)
    _day1(s, clock)
    _day2(s, clock, ids)
    _day3_hold(s, clock, ids)

    m = compute_monthly(s, year=2026, month=7)
    assert m == MonthlyAttribution(
        period="2026-07",
        sleeves=(
            SleeveMonth(sleeve="core", sessions=2, ret_pct=Decimal("0.00"),
                        benchmark_pct=Decimal("0.64"),
                        excess_pp=Decimal("-0.64"),
                        contribution_aud=Decimal("0.00"),
                        end_value_aud=Decimal("1500.00")),
            SleeveMonth(sleeve="xsmom", sessions=2, ret_pct=Decimal("2.84"),
                        benchmark_pct=Decimal("0.00"),
                        excess_pp=Decimal("2.84"),
                        contribution_aud=Decimal("234.74"),
                        end_value_aud=Decimal("8505.00")),
            SleeveMonth(sleeve="pead", sessions=0, ret_pct=None,
                        benchmark_pct=None, excess_pp=None,
                        contribution_aud=Decimal("0.00"),
                        end_value_aud=Decimal("0.00")),
            SleeveMonth(sleeve="cash", sessions=2, ret_pct=Decimal("0.00"),
                        benchmark_pct=Decimal("0.00"),
                        excess_pp=Decimal("0.00"),
                        contribution_aud=Decimal("0.00"),
                        end_value_aud=Decimal("91729.74")),
            SleeveMonth(sleeve="total", sessions=2, ret_pct=Decimal("0.23"),
                        benchmark_pct=Decimal("0.00"),
                        excess_pp=Decimal("0.23"),
                        contribution_aud=Decimal("234.74"),
                        end_value_aud=Decimal("101734.74")),
        ),
        nav_change_aud=Decimal("234.74"),
        satellite_alpha_pp=Decimal("2.84"),
        headline=("The active satellite added 2.84 pp vs simply holding the "
                  "index (SPY total return), cumulative since inception."))

    path = generate_monthly_report(s, clock, year=2026, month=7,
                                   reports_root=tmp_path)
    assert path == tmp_path / "attribution" / "2026-07.md"
    body = path.read_text()
    assert "# Attribution — 2026-07" in body
    assert m.headline in body                       # the honest one-liner
    assert ("Identity check (exact): sleeve contributions sum to "
            "A$234.74 = NAV change A$234.74.") in body
    assert "| core (55:15 SPY/INDA TR blend) | 2 | +0.00% | +0.64% |" in body
    assert "| xsmom (SPY TR) | 2 | +2.84% | +0.00% | +2.84 pp |" in body
    assert "Doc 04 §14 standing line" in body       # shortfall embedded

    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events WHERE event_type = :et "
        "AND entity_id = '2026-07'"), {"et": MONTHLY_REPORT_EVENT}).scalar()
    assert ev is not None
    assert ev["satellite_alpha_pp"] == "2.84"
    assert ev["nav_change_aud"] == "234.74"


def test_no_snapshot_yet_is_none_not_an_error(clean_audit):
    s = clean_audit
    _clean(s)
    assert compute_attribution_day(s, FrozenClock(T0)) is None
    assert backfill_attribution(s, FrozenClock(T0)) == []


# ---------------------------------------------------------------- API surface

@pytest.fixture
def aclient(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = clean_audit
    clock = FrozenClock(T0)
    ids = _seed_book(s, clock)
    _day1(s, clock)
    _day2(s, clock, ids)
    _day3_hold(s, clock, ids)
    s.commit()
    yield TestClient(app), s
    _clean(s)
    s.commit()
    reset_app_engine()


def test_api_daily_series_shape_and_exact_strings(aclient):
    c, _ = aclient
    r = c.get("/v1/portfolio/attribution/daily")
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 15
    d2core = next(x for x in body["rows"]
                  if x["session_date"] == "2026-07-14" and x["sleeve"] == "core")
    assert d2core == {"session_date": "2026-07-14", "sleeve": "core",
                      "value_aud": "1650.00", "ret_1d": "0.10000000",
                      "benchmark_ret_1d": "0.01428571"}
    d1core = next(x for x in body["rows"]
                  if x["session_date"] == "2026-07-13" and x["sleeve"] == "core")
    assert d1core["ret_1d"] is None and d1core["benchmark_ret_1d"] is None
    assert body["cumulative"]["xsmom"] == {
        "sessions": 2, "ret_pct": "2.84", "benchmark_pct": "0.00",
        "excess_pp": "2.84"}
    assert body["cumulative"]["core"] == {
        "sessions": 2, "ret_pct": "0.00", "benchmark_pct": "0.64",
        "excess_pp": "-0.64"}
    assert body["cumulative"]["pead"]["ret_pct"] is None
    assert body["satellite_alpha_pp"] == "2.84"
    # never scientific notation, even for a zero stored at scale 8
    d2cash = next(x for x in body["rows"]
                  if x["session_date"] == "2026-07-14" and x["sleeve"] == "cash")
    assert d2cash["ret_1d"] == "0.00000000"


def test_api_daily_and_monthly_routes_coexist(aclient):
    """Route-order regression: the literal /attribution/daily must not be
    swallowed by /attribution/{period}, and the monthly surface is unbroken."""
    c, _ = aclient
    assert c.get("/v1/portfolio/attribution/daily").status_code == 200
    monthly = c.get("/v1/portfolio/attribution/2026-07")
    assert monthly.status_code == 200
    assert monthly.json()["period"] == "2026-07"
    bad = c.get("/v1/portfolio/attribution/garbage")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_PERIOD"


def test_api_empty_series_answers_empty_not_404(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = clean_audit
    _clean(s)
    s.commit()
    try:
        r = TestClient(app).get("/v1/portfolio/attribution/daily")
        assert r.status_code == 200
        body = r.json()
        assert body["rows"] == []
        assert body["satellite_alpha_pp"] is None
        assert all(body["cumulative"][k]["ret_pct"] is None
                   for k in ("core", "xsmom", "pead", "cash", "total"))
    finally:
        reset_app_engine()


def test_cli_backfill_and_monthly_end_to_end(aclient, tmp_path):
    """`python -m atlas.dcp.reporting.attribution --backfill --month` against
    the committed book: idempotent re-upsert (the API fixture already
    computed the days), report file written, audit event committed."""
    _, s = aclient
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(
        [sys.executable, "-m", "atlas.dcp.reporting.attribution",
         "--backfill", "--month", "2026-07",
         "--now", "2026-07-16T00:00:00+00:00",
         "--reports-root", str(tmp_path)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "backfilled 3 session(s)" in r.stdout
    assert f"wrote {tmp_path / 'attribution' / '2026-07.md'}" in r.stdout
    assert (tmp_path / "attribution" / "2026-07.md").exists()
    s.expire_all()
    assert _rows(s) == GOLDEN_A                    # still the goldens, exactly
    n = s.execute(text(
        "SELECT count(*) FROM audit.decision_events WHERE event_type = :et"),
        {"et": MONTHLY_REPORT_EVENT}).scalar()
    assert n == 1
