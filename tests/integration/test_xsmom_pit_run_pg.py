"""run_xsmom_pit smoke on atlas_test with seeded fixtures (no live calls):
eleven fake INACTIVE members + an active SPY benchmark, membership rows in
validation.index_membership — through the FULL runner path: point-in-time
panel (dead series KEPT), membership-gated eligibility, the delisting rule
firing on a name that dies mid-window, ONE registered trial in family
'xsmom-pit', SPY benchmark excluded from the ranked universe, the audit event,
and the report with the definitive-test header, verbatim verdict and the
annual-outcome-distribution house rule.

Every price bar and membership row is deleted INSIDE the test transaction
(rolled back at teardown), so the panel sees exactly this test's fixtures."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.registry import trial_count
from atlas.dcp.backtest.xsmom_pit_run import (
    load_pit_panel,
    pit_eligible,
    render_pit_report,
    run_xsmom_pit,
)
from atlas.dcp.backtest.portfolio import PanelView
from atlas.dcp.market_data.calendars import trading_days_between
from tests.conftest import requires_pg

pytestmark = requires_pg

SESSIONS = trading_days_between("US", date(2011, 1, 3), date(2013, 12, 31))
MEMBERS = [f"ZPT{k}" for k in range(10)]          # long-standing members
DEAD = "ZPTDEAD"                                   # dies 2013-08-30, best drift
LATE = "ZPTLATE"                                   # joins 2013-03-01
NULLDEAD = "ZPTNULLX"                              # null start + delisted: excluded
DEAD_LAST = date(2013, 8, 30)
FETCHED = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)


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


def _seed(s) -> None:
    s.execute(text("DELETE FROM market.price_bars_daily"))       # in-txn only
    s.execute(text("DELETE FROM validation.index_membership"))
    s.execute(text("DELETE FROM quant.trial_registry "
                   "WHERE strategy_family = 'xsmom-pit'"))
    _bars(s, _instrument(s, "SPY", active=True), 0.0004)
    epoch = date(2005, 1, 3)
    for k, sym in enumerate(MEMBERS):
        _bars(s, _instrument(s, sym, active=False), -0.0006 + 0.0002 * k)
        _member_row(s, sym, epoch if k % 2 else None, None,
                    active=True, delisted=False)   # half with null StartDate
    # DEAD: highest drift so it is always held; series and membership both end
    _bars(s, _instrument(s, DEAD, active=False), 0.0016, last=DEAD_LAST)
    _member_row(s, DEAD, epoch, date(2013, 9, 2), active=False, delisted=True)
    # LATE: full series, but only a member from 2013-03-01
    _bars(s, _instrument(s, LATE, active=False), 0.0012)
    _member_row(s, LATE, date(2013, 3, 1), None, active=True, delisted=False)
    # NULLDEAD: bars exist but the row is excluded fail-closed (null start,
    # not active) — it must never enter the panel's ranked universe
    _bars(s, _instrument(s, NULLDEAD, active=False), 0.0010)
    _member_row(s, NULLDEAD, None, date(2013, 6, 2), active=False, delisted=True)


def test_pit_runner_full_path(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22, tzinfo=UTC)))

    run = run_xsmom_pit(s, audit, paths=10, seed=7)

    # panel: members only (dead series KEPT), benchmark outside the universe,
    # fail-closed exclusion honoured
    assert set(run.universe.members) == set(MEMBERS) | {DEAD, LATE}
    assert NULLDEAD not in run.universe.members
    assert "SPY" not in run.universe.members
    assert run.universe.included_delisted == 1
    assert run.universe.partition.excluded_null_start_delisted != ()

    # evaluation window starts at the documented membership-reliability bound
    assert run.start == date(2012, 7, 2)

    # the delisting rule fired: DEAD died mid-window while held (top drift)
    assert any(f.symbol == DEAD for f in run.run.forced_liquidations)
    dead_day = min(f.day for f in run.run.forced_liquidations
                   if f.symbol == DEAD)
    assert dead_day == date(2013, 9, 3)      # first session without a bar

    # point-in-time eligibility on the loaded panel: LATE ranks only once a
    # member; DEAD disappears with its series
    panel, members = run.universe.panel, run.universe.members
    feb2013 = panel.dates.index(date(2013, 2, 28))
    mar2013 = panel.dates.index(date(2013, 3, 28))
    oct2013 = panel.dates.index(date(2013, 10, 31))
    assert LATE not in pit_eligible(PanelView(panel, feb2013), members)
    assert LATE in pit_eligible(PanelView(panel, mar2013), members)
    assert DEAD in pit_eligible(PanelView(panel, feb2013), members)
    assert DEAD not in pit_eligible(PanelView(panel, oct2013), members)

    # ONE trial, family xsmom-pit, true count feeds the gate
    assert trial_count(s, "xsmom-pit") == 1
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
        "AND entity_id = 'xsmom-pit/portfolio' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload is not None
    assert payload["gate_passed"] == run.gate.passed
    assert payload["forced_liquidations"] == len(run.run.forced_liquidations)
    assert "point-in-time" in payload["universe"]

    # report: definitive-test header, delisting rule, verbatim verdict, the
    # annual-outcome-distribution house rule
    report = render_pit_report(run, paths=10)
    assert "WHY THIS IS THE DEFINITIVE TEST" in report
    assert "DELISTING RULE" in report
    assert "RECONSTRUCTION UNDERCOUNT" in report
    assert "## Annual outcome distribution" in report
    if run.gate.passed:
        assert "**PASS**" in report
        assert "History is not a forecast" in report
    else:
        assert "**FAIL**" in report
        assert "No distribution is derived for a failed strategy" in report
        for reason in run.gate.reasons:
            assert reason in report


def test_pit_panel_keeps_dead_series_and_gates_membership(pg_session):
    """load_pit_panel keeps a series that ends early (the frozen loader would
    exclude it as delisting-shaped) and refuses nothing silently: NULLDEAD's
    stored bars exist, yet it is absent from the members map because its
    membership row is excluded fail-closed."""
    s = pg_session
    _seed(s)
    uni = load_pit_panel(s)
    assert DEAD in uni.panel.closes
    dead_idx = uni.panel.dates.index(DEAD_LAST)
    assert uni.panel.closes[DEAD][dead_idx] is not None
    assert uni.panel.closes[DEAD][dead_idx + 1] is None
    assert NULLDEAD not in uni.members
    assert uni.window_members == len(MEMBERS) + 2
    assert uni.missing_series == []
