"""run_quality_pit smoke on the isolated test DB with seeded fixtures (no live
calls): member instruments carrying quarterly fundamentals with monotone GP/A
levels, one member with prices but NO fundamentals (ineligible), one
Financials-sector member (the -xfin flag's target), an active SPY benchmark,
and a delisted member that dies mid-window. Exercises the FULL runner path —
point-in-time price panel + fundamentals panel, GP/A eligibility gated by
membership AND a live fresh defined signal, top-decile winner ranking, ONE
registered 'quality-gpa' trial, SPY excluded from the ranked universe, the
audit event, the report, and the explicit (never default) financials
exclusion registering its own '-xfin' family.

Every fixture row is written INSIDE the test transaction (rolled back at
teardown), so the panel sees exactly this test's data."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.portfolio import PanelView
from atlas.dcp.backtest.quality_pit_run import (
    family_name,
    load_quality_signals,
    quality_pit_eligible,
    quality_pit_strategy,
    render_quality_report,
    run_quality_pit,
)
from atlas.dcp.backtest.registry import trial_count
from atlas.dcp.market_data.calendars import trading_days_between
from tests.conftest import requires_pg

pytestmark = requires_pg

SESSIONS = trading_days_between("US", date(2010, 1, 4), date(2013, 12, 31))
RANKED = [f"ZQ{k}" for k in range(12)]        # GP/A levels 0.05*k -> monotone
DEAD = "ZQDEAD"                               # high GP/A; dies mid-window
NOFUND = "ZQNOFUND"                           # member + prices, no fundamentals
FIN = "ZQFIN"                                 # Financials sector, highest GP/A
DEAD_LAST = date(2013, 8, 30)
FETCHED = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)


def _instrument(s, symbol: str, *, active: bool, sector: str = "Broad") -> str:
    existing = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = :sym"),
        {"sym": symbol}).scalar()
    if existing is not None:
        s.execute(text("UPDATE market.instruments SET is_active = :act, "
                       "sector_gics = :sec WHERE id = :iid"),
                  {"iid": existing, "act": active, "sec": sector})
        return str(existing)
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, :sec, 'USD', :act) "
        "RETURNING id"), {"sym": symbol, "act": active, "sec": sector}).scalar())


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


def _fundamentals(s, iid: str, level: float, *, n: int = 16,
                  last_filing: date | None = None) -> None:
    """Quarterly statements (fpe stepping exactly 91 days from 2010-03-31 —
    consecutive by construction; filing_date = fpe + 40 days). Every quarter:
    gross_profit = 25*level, total_assets = 100, so trailing-4Q GP/A == level
    exactly — monotone in the level, higher-level names rank strictly higher."""
    fpe0 = date(2010, 3, 31)
    rows = []
    for q in range(n):
        fpe = fpe0 + timedelta(days=91 * q)
        fd = fpe + timedelta(days=40)
        if last_filing is not None and fd > last_filing:
            break
        rows.append({"iid": iid, "fpe": fpe, "fd": fd,
                     "gp": round(25.0 * level, 6), "tr": round(50.0 * level, 6),
                     "ta": 100.0, "fa": FETCHED})
    s.execute(text(
        "INSERT INTO market.quarterly_fundamentals (instrument_id, "
        "fiscal_period_end, filing_date, gross_profit, total_revenue, "
        "total_assets, currency, source, fetched_at) "
        "VALUES (:iid, :fpe, :fd, :gp, :tr, :ta, 'USD', 'EodhdAdapter', :fa)"),
        rows)


def _seed(s) -> None:
    s.execute(text("DELETE FROM market.price_bars_daily"))          # in-txn only
    s.execute(text("DELETE FROM market.quarterly_fundamentals"))
    s.execute(text("DELETE FROM validation.index_membership"))
    s.execute(text("DELETE FROM quant.trial_registry "
                   "WHERE lineage = 'quality'"))    # ADR-0016 lineage isolation
    _bars(s, _instrument(s, "SPY", active=True), 0.0004)            # benchmark
    for k, sym in enumerate(RANKED):
        iid = _instrument(s, sym, active=False)
        _bars(s, iid, 0.0002)
        _fundamentals(s, iid, 0.05 * k)
        _member_row(s, sym, date(2005, 1, 3), None, active=True, delisted=False)
    # DEAD: high GP/A (always held while eligible); series + membership end
    dead = _instrument(s, DEAD, active=False)
    _bars(s, dead, 0.0003, last=DEAD_LAST)
    _fundamentals(s, dead, 2.0, last_filing=DEAD_LAST)
    _member_row(s, DEAD, date(2005, 1, 3), date(2013, 9, 2),
                active=False, delisted=True)
    # NOFUND: member with prices but NO statements -> never a live signal
    nofund = _instrument(s, NOFUND, active=False)
    _bars(s, nofund, 0.0001)
    _member_row(s, NOFUND, date(2005, 1, 3), None, active=True, delisted=False)
    # FIN: Financials sector, HIGHEST GP/A — ranked by default, excluded by -xfin
    fin = _instrument(s, FIN, active=False, sector="Financials")
    _bars(s, fin, 0.0002)
    _fundamentals(s, fin, 3.0)
    _member_row(s, FIN, date(2005, 1, 3), None, active=True, delisted=False)


def test_quality_runner_full_path(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22, tzinfo=UTC)))

    run = run_quality_pit(s, audit, paths=6, seed=7)

    # universe: members only (dead series kept), SPY outside the ranked universe
    assert set(run.universe.members) == set(RANKED) | {DEAD, NOFUND, FIN}
    assert "SPY" not in run.universe.members
    assert run.start == date(2012, 7, 2)
    assert run.family == "quality-gpa"
    assert run.excluded_financials == ()          # default: NO silent exclusion

    # fundamentals coverage: ranked + DEAD + FIN carry quarters; NOFUND does not
    assert run.coverage.symbols_with_fundamentals == len(RANKED) + 2
    assert run.coverage.delisted_with_fundamentals == 1              # DEAD
    assert run.coverage.total_quarters > 0

    # rebuild the point-in-time signal view to probe eligibility directly
    panel, members = run.universe.panel, run.universe.members
    fundamentals, _cov = load_quality_signals(s, sorted(members), panel.dates,
                                              members)
    dec2012 = panel.dates.index(date(2012, 12, 31))
    elig = quality_pit_eligible(PanelView(panel, dec2012), fundamentals, members)
    assert NOFUND not in elig                 # no statements => no live signal
    assert set(RANKED).issubset(set(elig))    # all fundamentals names eligible
    assert DEAD in elig and FIN in elig       # financials ranked BY DEFAULT

    # GP/A ranking -> winner decile: 14 eligible => winner_count 10, so the
    # four LOWEST-GP/A names (ZQ0..ZQ3) are dropped; FIN and DEAD lead
    strat = quality_pit_strategy(members, fundamentals)
    holdings = set(strat(PanelView(panel, dec2012)))
    assert len(holdings) == 10
    assert FIN in holdings and DEAD in holdings and "ZQ11" in holdings
    assert {"ZQ0", "ZQ1", "ZQ2", "ZQ3"}.isdisjoint(holdings)

    # the delisting path fired for DEAD (forced liquidation or unfilled buy)
    touched = ({f.symbol for f in run.run.forced_liquidations}
               | {sym for _, sym in run.run.unfilled_buys})
    assert DEAD in touched

    # ONE trial, family quality-gpa, true count feeds the gate
    assert trial_count(s, "quality-gpa") == 1
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
        "AND entity_id = 'quality-gpa/portfolio' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload is not None
    assert payload["gate_passed"] == run.gate.passed
    assert payload["signal"].startswith("GP/A")
    assert payload["financials_excluded"] is False
    assert "point-in-time" in payload["universe"]

    # report: header, no-look-ahead claim, the financials honesty note, verdict,
    # verbatim reasons, house rule — and no robustness boilerplate on a FAIL
    report = render_quality_report(run, paths=6)
    assert "STRATEGY CANDIDATE #3" in report
    assert "NO LOOK-AHEAD IS STRUCTURAL" in report
    assert "Novy-Marx 2013 EXCLUDES financial firms" in report
    assert "does NOT exclude them" in report
    assert "## Annual outcome distribution" in report
    if run.gate.passed:
        assert "**PASS**" in report
    else:
        assert "**FAIL**" in report
        assert "No distribution is derived for a failed strategy" in report
        for reason in run.gate.reasons:
            assert reason in report


def test_quality_runner_exclude_financials_is_second_explicit_trial(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22, tzinfo=UTC)))

    run = run_quality_pit(s, audit, paths=4, seed=7, exclude_financials=True)

    assert run.family == "quality-gpa-xfin"       # its OWN registered family
    assert run.excluded_financials == (FIN,)
    assert trial_count(s, "quality-gpa-xfin") == 1
    assert trial_count(s, "quality-gpa") == 0     # the default family untouched

    # FIN is out of the eligible set and the holdings; DEAD/ranked unaffected
    panel, members = run.universe.panel, run.universe.members
    fundamentals, _cov = load_quality_signals(s, sorted(members), panel.dates,
                                              members)
    dec2012 = panel.dates.index(date(2012, 12, 31))
    elig = quality_pit_eligible(PanelView(panel, dec2012), fundamentals, members,
                                excluded=frozenset(run.excluded_financials))
    assert FIN not in elig and DEAD in elig
    strat = quality_pit_strategy(members, fundamentals,
                                 excluded=frozenset(run.excluded_financials))
    holdings = set(strat(PanelView(panel, dec2012)))
    assert FIN not in holdings and DEAD in holdings

    report = render_quality_report(run, paths=4)
    assert "financials excluded" in report
    assert "THIS run applies that exclusion explicitly" in report


def test_window_start_requires_total_return_mode(pg_session):
    s = pg_session
    audit = PostgresAuditLog(s, FrozenClock(datetime(2013, 12, 31, 22, tzinfo=UTC)))
    with pytest.raises(ValueError):
        run_quality_pit(s, audit, paths=1, window_start=date(2016, 1, 1))


def test_family_name_composition():
    assert family_name(total_return=False, exclude_financials=False,
                       window_start=None) == "quality-gpa"
    assert family_name(total_return=True, exclude_financials=False,
                       window_start=None) == "quality-gpa-tr"
    assert family_name(total_return=True, exclude_financials=True,
                       window_start=None) == "quality-gpa-tr-xfin"
    assert family_name(total_return=True, exclude_financials=False,
                       window_start=date(2016, 1, 1)) == "quality-gpa-tr-2016"
