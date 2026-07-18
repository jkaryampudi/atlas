"""Total-return mode of the PIT runner on atlas_test fixtures (no live calls):
the loader-level TR transform (dividends reinvested at ex-date close, applied
to the ONE panel every engine consumer reads), the SPY-must-have-dividends
fail-loud, family plumbing ('xsmom-pit-tr' / 'xsmom-pit-tr-<year>' kill
tests), the null-stream identity between pit_null_results and
pit_null_distribution, the exact verdict-vs-endpoint truncation, and the
combined supersession report.

Seeding mirrors test_xsmom_pit_run_pg (same fake members, same window); every
price bar and membership row is deleted INSIDE the test transaction."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.real_run import COSTS
from atlas.dcp.backtest.registry import trial_count
from atlas.dcp.backtest.xsmom_pit_run import (
    FAMILY_TR,
    KILL_START,
    load_pit_panel,
    pit_null_distribution,
    render_tr_report,
    run_xsmom_pit,
    tr_family,
    verdict_vs_endpoint,
)
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.ingest import record_dividend
from atlas.dcp.market_data.models import Dividend
from tests.conftest import requires_pg

pytestmark = requires_pg

SESSIONS = trading_days_between("US", date(2011, 1, 3), date(2013, 12, 31))
MEMBERS = [f"ZPT{k}" for k in range(10)]
DEAD = "ZPTDEAD"                                   # dies 2013-08-30
DEAD_LAST = date(2013, 8, 30)
FETCHED = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
SPY_DIV_DATES = [d for d in (date(2012, 3, 16), date(2012, 6, 15),
                             date(2013, 3, 15), date(2013, 6, 21))]


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


def _dividend(s, symbol: str, ex: date, amount: str) -> None:
    iid = s.execute(text("SELECT id FROM market.instruments "
                         "WHERE symbol = :sym"), {"sym": symbol}).scalar()
    record_dividend(s, iid, Dividend(symbol=symbol, ex_date=ex,
                                     amount=Decimal(amount), currency="USD"),
                    "EodhdAdapter")


def _seed(s, *, spy_dividends: bool = True) -> None:
    s.execute(text("DELETE FROM market.price_bars_daily"))       # in-txn only
    s.execute(text("DELETE FROM market.corporate_actions"))
    s.execute(text("DELETE FROM validation.index_membership"))
    s.execute(text("DELETE FROM quant.trial_registry "
                   "WHERE lineage = 'momentum'"))   # ADR-0016 lineage isolation
    _bars(s, _instrument(s, "SPY", active=True), 0.0004)
    epoch = date(2005, 1, 3)
    for k, sym in enumerate(MEMBERS):
        _bars(s, _instrument(s, sym, active=False), -0.0006 + 0.0002 * k)
        _member_row(s, sym, epoch if k % 2 else None, None,
                    active=True, delisted=False)
    # DEAD: highest drift so it is always held; series and membership both end
    _bars(s, _instrument(s, DEAD, active=False), 0.0016, last=DEAD_LAST)
    _member_row(s, DEAD, epoch, date(2013, 9, 2), active=False, delisted=True)
    if spy_dividends:
        for ex in SPY_DIV_DATES:
            _dividend(s, "SPY", ex, "0.80")
    # a member with dividends, one dropped-after-final-bar case, one before
    # inception, one never-payer set (the rest have none — normal)
    _dividend(s, "ZPT9", date(2012, 9, 21), "1.50")
    _dividend(s, DEAD, date(2013, 9, 15), "9.99")   # after final bar: dropped
    _dividend(s, "ZPT8", date(2010, 6, 1), "9.99")  # before inception: dropped


def test_tr_panel_scales_series_and_counts_coverage(pg_session):
    s = pg_session
    _seed(s)
    pr = load_pit_panel(s)                       # price panel: the reference
    tr = load_pit_panel(s, total_return=True)
    assert pr.tr is None and tr.tr is not None

    # SPY: factor jumps by exactly 1 + amount/price_close at each ex-date and
    # is flat elsewhere (hand-derivable from the price panel)
    dates = pr.panel.dates
    p_close = pr.panel.closes["SPY"]
    t_close = tr.panel.closes["SPY"]
    factor = 1.0
    for i, d in enumerate(dates):
        if p_close[i] is None:
            continue
        if d in SPY_DIV_DATES:
            factor *= 1.0 + 0.80 / p_close[i]
        assert t_close[i] == pytest.approx(p_close[i] * factor)
    assert factor > 1.0                           # dividends actually applied

    # never-payer stays byte-identical to the price series
    assert tr.panel.closes["ZPT0"] == pr.panel.closes["ZPT0"]

    cov = tr.tr
    assert cov.spy_dividends == len(SPY_DIV_DATES)
    assert cov.symbols_with_dividends == 2        # SPY + ZPT9
    assert cov.dividends_applied == len(SPY_DIV_DATES) + 1
    assert cov.dropped_after_series == 1          # DEAD's post-delisting cash
    assert cov.dropped_before_series == 1         # ZPT8's pre-inception row
    assert cov.rolled_forward == 0


def test_tr_mode_refuses_benchmark_without_dividends(pg_session):
    """A TR benchmark without SPY's yield re-creates the original defect —
    fail loud, never a silently price-return SPY."""
    s = pg_session
    _seed(s, spy_dividends=False)
    with pytest.raises(RuntimeError, match="SPY has no stored dividends"):
        load_pit_panel(s, total_return=True)


def test_tr_runner_full_path_families_nulls_and_exhibits(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22, tzinfo=UTC)))

    run = run_xsmom_pit(s, audit, paths=5, seed=7, total_return=True)

    # family and convention plumbing; ONE registered trial in the TR family
    assert run.family == FAMILY_TR == "xsmom-pit-tr"
    assert "total return" in run.return_convention
    assert trial_count(s, FAMILY_TR) == 1
    assert trial_count(s, "xsmom-pit") == 0       # the price family untouched
    assert run.start == date(2012, 7, 2)          # identical window
    assert run.universe.tr is not None
    assert run.wf_spy is not None
    assert len(run.wf_spy.fold_results) == len(run.wf.fold_results)

    # null-stream identity: pit_null_results is pit_null_distribution with
    # curves kept — same seed, same draws, same totals, element for element
    nulls = pit_null_distribution(run.universe.panel, run.universe.members,
                                  costs=COSTS, start=run.start, paths=5, seed=7)
    assert [r.total_return for r in run.null_results] == nulls

    # the audit event records the convention
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'quant.backtest.completed' "
        "AND entity_id = 'xsmom-pit-tr/portfolio' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload is not None
    assert "total return" in payload["return_convention"]

    # endpoint exhibit: exact truncation — the final row IS the gate
    eps = verdict_vs_endpoint(run, months=3)
    assert len(eps) == 4                          # 3 rollbacks + the final date
    assert [e.endpoint for e in eps] == sorted(e.endpoint for e in eps)
    last = eps[-1]
    assert last.endpoint == run.run.result.dates[-1]
    assert last.strategy_return == run.gate.strategy_return
    assert last.spy_return == run.gate.spy_bh_return
    assert last.null_p == run.gate.null_p_value
    assert last.dsr == pytest.approx(run.gate.dsr, rel=1e-12)
    assert last.beats_spy == (last.strategy_return > last.spy_return)

    # kill-test plumbing: pre-committed start override, its own family
    kill = run_xsmom_pit(s, audit, paths=5, seed=7, total_return=True,
                         window_start=date(2013, 1, 1))
    assert kill.family == tr_family(date(2013, 1, 1)) == "xsmom-pit-tr-2013"
    assert kill.start == date(2013, 1, 2)         # first session on/after it
    assert trial_count(s, "xsmom-pit-tr-2013") == 1
    assert kill.trials_after_total == run.trials_after_total + 1
    # the board's real kill test is pinned at 2016-01-01
    assert KILL_START == date(2016, 1, 1)

    # combined report: header, supersession EITHER WAY, verbatim verdicts,
    # both exhibits, house rule on the earnings profile
    report = render_tr_report(run, kill, paths=5)
    assert "WHY THIS TEST EXISTS" in report
    assert "superseded by this" in report
    assert "verdict vs endpoint" in report
    assert "per-calendar-year total returns" in report
    assert "SPY TR (same fold)" in report
    for r in (run, kill):
        assert f"**{'PASS' if r.gate.passed else 'FAIL'}**" in report
        for reason in r.gate.reasons:
            assert reason in report
    if run.gate.passed:
        assert "History is not a forecast" in report
    else:
        assert "No distribution is derived for a failed strategy" in report


def test_window_start_guards(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22, tzinfo=UTC)))
    with pytest.raises(ValueError, match="only in total-return mode"):
        run_xsmom_pit(s, audit, paths=2, window_start=date(2013, 1, 1))
    with pytest.raises(ValueError, match="membership-reliability bound"):
        run_xsmom_pit(s, audit, paths=2, total_return=True,
                      window_start=date(2012, 1, 1))


def test_price_mode_unchanged_by_tr_machinery(pg_session):
    """The default path must be byte-identical to the pre-board runner: no TR
    coverage, price convention, no stored null curves, family 'xsmom-pit'."""
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22, tzinfo=UTC)))
    run = run_xsmom_pit(s, audit, paths=3, seed=7)
    assert run.family == "xsmom-pit"
    assert run.universe.tr is None
    assert run.null_results == ()
    assert run.wf_spy is None
    assert "price" in run.return_convention
    with pytest.raises(ValueError, match="needs stored null curves"):
        verdict_vs_endpoint(run)
