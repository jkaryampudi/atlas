"""run_pead_pit smoke on atlas_test with seeded fixtures (no live calls):
member instruments carrying quarterly earnings surprises, one member with
prices but NO earnings (ineligible), an active SPY benchmark, and a delisted
member that dies mid-window. Exercises the FULL runner path — point-in-time
price panel + earnings panel, SUE eligibility gated by membership AND a live
fresh surprise, top-decile winner ranking, ONE registered 'pead-sue' trial,
SPY excluded from the ranked universe, the audit event, and the report.

Every fixture row is written INSIDE the test transaction (rolled back at
teardown), so the panel sees exactly this test's data."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.portfolio import PanelView
from atlas.dcp.backtest.pead_pit_run import (
    load_pead_signals,
    pead_pit_eligible,
    pead_pit_strategy,
    render_pead_report,
    run_pead_pit,
)
from atlas.dcp.backtest.registry import trial_count
from atlas.dcp.market_data.calendars import trading_days_between
from tests.conftest import requires_pg

pytestmark = requires_pg

SESSIONS = trading_days_between("US", date(2010, 1, 4), date(2013, 12, 31))
RANKED = [f"ZP{k}" for k in range(12)]        # levels 0.05*k -> monotone SUE
DEAD = "ZPDEAD"                               # highest SUE; dies mid-window
NOEARN = "ZPNOEARN"                           # member + prices, but no earnings
DEAD_LAST = date(2013, 8, 30)
FETCHED = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def _instrument(s, symbol: str, *, active: bool) -> str:
    existing = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = :sym"),
        {"sym": symbol}).scalar()
    if existing is not None:
        s.execute(text("UPDATE market.instruments SET is_active = :act "
                       "WHERE id = :iid"), {"iid": existing, "act": active})
        return str(existing)
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'Broad', 'USD', :act) "
        "RETURNING id"), {"sym": symbol, "act": active}).scalar())


def _bars(s, iid: str, rate: float, *, last: date | None = None) -> None:
    days = [d for d in SESSIONS if last is None or d <= last]
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, 1000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": round(100.0 * math.exp(rate * i), 6)}
         for i, d in enumerate(days)])


def _member_row(s, ticker: str, start: date | None, end: date | None, *,
                active: bool, delisted: bool) -> None:
    s.execute(text(
        "INSERT INTO validation.index_membership "
        "(index_code, ticker, name, start_date, end_date, is_active_now, "
        " is_delisted, fetched_at) "
        "VALUES ('GSPC.INDX', :t, :t, :sd, :ed, :a, :dl, :f)"),
        {"t": ticker, "sd": start, "ed": end, "a": active, "dl": delisted,
         "f": FETCHED})


def _earnings(s, iid: str, level: float, *, n: int = 16,
             last_report: date | None = None) -> None:
    """Quarterly reports (fpe stepping ~91 days from 2010-03-31, report_date =
    fpe + 20 days). estimate 1.00; surprise = level + alternating ±0.01 so the
    within-name dispersion is non-zero and SUE ~ 5*level/0.01 — monotone in the
    level, so higher-level names rank strictly higher."""
    fpe0 = date(2010, 3, 31)
    rows = []
    for q in range(n):
        fpe = fpe0 + timedelta(days=91 * q)
        rd = fpe + timedelta(days=20)
        if last_report is not None and rd > last_report:
            break
        surprise = level + (0.01 if q % 2 == 0 else -0.01)
        rows.append({"iid": iid, "fpe": fpe, "rd": rd,
                     "a": round(1.00 + surprise, 6), "e": 1.00,
                     "baf": "BeforeMarket", "fa": FETCHED})
    s.execute(text(
        "INSERT INTO market.earnings_surprises (instrument_id, fiscal_period_end, "
        "report_date, eps_actual, eps_estimate, surprise_pct, currency, "
        "before_after_market, source, fetched_at) "
        "VALUES (:iid, :fpe, :rd, :a, :e, NULL, 'USD', :baf, 'EodhdAdapter', :fa)"),
        rows)


def _seed(s) -> None:
    s.execute(text("DELETE FROM market.price_bars_daily"))          # in-txn only
    s.execute(text("DELETE FROM market.earnings_surprises"))
    s.execute(text("DELETE FROM validation.index_membership"))
    s.execute(text("DELETE FROM quant.trial_registry "
                   "WHERE lineage = 'pead'"))       # ADR-0016 lineage isolation
    _bars(s, _instrument(s, "SPY", active=True), 0.0004)            # benchmark
    for k, sym in enumerate(RANKED):
        iid = _instrument(s, sym, active=False)
        _bars(s, iid, 0.0002)
        _earnings(s, iid, 0.05 * k)
        _member_row(s, sym, date(2005, 1, 3), None, active=True, delisted=False)
    # DEAD: highest SUE (always held while eligible); series + membership end
    dead = _instrument(s, DEAD, active=False)
    _bars(s, dead, 0.0003, last=DEAD_LAST)
    _earnings(s, dead, 1.0, last_report=DEAD_LAST)
    _member_row(s, DEAD, date(2005, 1, 3), date(2013, 9, 2),
                active=False, delisted=True)
    # NOEARN: member with prices but NO earnings -> never a live signal
    noearn = _instrument(s, NOEARN, active=False)
    _bars(s, noearn, 0.0001)
    _member_row(s, NOEARN, date(2005, 1, 3), None, active=True, delisted=False)


def test_pead_runner_full_path(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22, tzinfo=UTC)))

    run = run_pead_pit(s, audit, paths=10, seed=7)

    # universe: members only (dead series kept), SPY outside the ranked universe
    assert set(run.universe.members) == set(RANKED) | {DEAD, NOEARN}
    assert "SPY" not in run.universe.members
    assert run.start == date(2012, 7, 2)

    # earnings coverage: every ranked name + DEAD carries reports; NOEARN does not
    assert run.coverage.symbols_with_reports == len(RANKED) + 1     # + DEAD
    assert run.coverage.delisted_with_reports == 1                  # DEAD
    assert run.coverage.total_reports > 0

    # rebuild the point-in-time signal view to probe eligibility directly
    panel, members = run.universe.panel, run.universe.members
    earnings, _cov = load_pead_signals(s, sorted(members), panel.dates, members)
    dec2012 = panel.dates.index(date(2012, 12, 31))
    elig = pead_pit_eligible(PanelView(panel, dec2012), earnings, members)
    assert NOEARN not in elig                       # no earnings => no live signal
    assert set(RANKED).issubset(set(elig))          # all earnings names eligible
    assert DEAD in elig

    # SUE ranking -> winner decile: 13 eligible (12 + DEAD) => winner_count 10,
    # so the three LOWEST-SUE names (ZP0, ZP1, ZP2) are dropped, DEAD held
    strat = pead_pit_strategy(members, earnings)
    holdings = set(strat(PanelView(panel, dec2012)))
    assert len(holdings) == 10
    assert DEAD in holdings and "ZP11" in holdings
    assert {"ZP0", "ZP1", "ZP2"}.isdisjoint(holdings)

    # the delisting path fired for DEAD (forced liquidation or unfilled buy)
    touched = ({f.symbol for f in run.run.forced_liquidations}
               | {sym for _, sym in run.run.unfilled_buys})
    assert DEAD in touched

    # ONE trial, family pead-sue, true count feeds the gate
    assert trial_count(s, "pead-sue") == 1
    assert run.n_trials == 1 and run.gate.n_trials == 1
    assert run.trials_after_total == run.trials_before_total + 1

    # engine really traded; SPY benchmark ran over the same window
    assert run.run.result.n_rebalances >= 10
    assert run.spy.total_return != 0.0
    assert run.gate.spy_bh_return == run.spy.total_return
    assert len(run.wf.fold_results) == 4

    # audit event
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'quant.backtest.completed' "
        "AND entity_id = 'pead-sue/portfolio' ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload is not None
    assert payload["gate_passed"] == run.gate.passed
    assert payload["signal"].startswith("SUE")
    assert "point-in-time" in payload["universe"]

    # report: header, no-look-ahead claim, verdict, verbatim reasons, house rule
    report = render_pead_report(run, paths=10)
    assert "THE ONE ORTHOGONAL FACTOR" in report
    assert "NO LOOK-AHEAD IS STRUCTURAL" in report
    assert "## Annual outcome distribution" in report
    if run.gate.passed:
        assert "**PASS**" in report
    else:
        assert "**FAIL**" in report
        assert "No distribution is derived for a failed strategy" in report
        for reason in run.gate.reasons:
            assert reason in report
