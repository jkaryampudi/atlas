"""run_xsmom symbols mode (survivorship cross-check) on atlas_test: nine fake
INACTIVE sector-fund stand-ins + an active SPY, through the FULL runner path —
panel restricted to exactly the requested symbols, proportional top-3, ONE
registered trial in the requested family, SPY benchmark on a SIDE PANEL with
an identical session axis (SPY never enters the ranked universe), the audit
event, and the validation report with the survivorship-free header, the
verbatim verdict and the annual-outcome-distribution house rule.

Every price bar is deleted INSIDE the test transaction (rolled back at
teardown), so the panel sees exactly this test's instruments."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.registry import trial_count
from atlas.dcp.backtest.xsmom_run import render_etf_report, run_xsmom
from atlas.dcp.market_data.calendars import trading_days_between
from tests.conftest import requires_pg

pytestmark = requires_pg

SESSIONS = trading_days_between("US", date(2024, 1, 2), date(2025, 6, 30))
ETFS = [f"ZET{k}" for k in range(9)]


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
        "VALUES (:sym, 'XTEST', 'US', 'etf', :sym, 'Broad', 'USD', :act) "
        "RETURNING id"), {"sym": symbol, "act": active}).scalar())


def _bars(s, iid: str, rate: float) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, 1000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": round(100.0 * math.exp(rate * i), 6)}
         for i, d in enumerate(SESSIONS)])


def _seed(s) -> None:
    s.execute(text("DELETE FROM market.price_bars_daily"))       # in-txn only
    s.execute(text("DELETE FROM quant.trial_registry "
                   "WHERE strategy_family = 'xsmom-etf'"))
    _bars(s, _instrument(s, "SPY", active=True), 0.0004)
    for k, sym in enumerate(ETFS):                # distinct drifts -> ranking
        _bars(s, _instrument(s, sym, active=False), -0.001 + 0.0003 * k)


def test_symbols_mode_full_runner_path(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2025, 6, 30, 22, tzinfo=UTC)))

    run = run_xsmom(s, audit, paths=15, seed=7,
                    symbols=list(ETFS), top_n=3, family="xsmom-etf")

    # panel: exactly the nine requested (inactive) names; SPY stays out of
    # the ranked universe and arrives via the side panel
    assert run.universe.included == sorted(ETFS)
    assert "SPY" not in run.universe.included
    assert run.family == "xsmom-etf" and run.top_n == 3
    assert run.spy.total_return != 0.0
    assert run.gate.spy_bh_return == run.spy.total_return
    assert run.gate.ew_return != run.gate.spy_bh_return

    # ONE trial, requested family, true count feeds the gate
    assert trial_count(s, "xsmom-etf") == 1
    assert run.n_trials == 1 and run.gate.n_trials == 1
    assert run.trials_after_total == run.trials_before_total + 1

    # engine really traded; walk-forward ran with real_run constants
    assert run.result.n_rebalances >= 3
    assert run.start == run.universe.panel.dates[252]
    assert len(run.wf.fold_results) == 4

    # audit: family-keyed entity, survivorship-free caveat
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'quant.backtest.completed' "
        "AND entity_id = 'xsmom-etf/portfolio' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload is not None
    assert payload["gate_passed"] == run.gate.passed
    assert "survivorship-free" in payload["survivorship_caveat"]

    # report: survivorship-free rationale, implications, verdict verbatim,
    # annual-distribution house rule (dispersion only for a validated pass)
    report = render_etf_report(run, paths=15)
    assert "WHY THIS UNIVERSE IS SURVIVORSHIP-FREE" in report
    assert "Moskowitz & Grinblatt (1999)" in report
    assert "top 3 of 9" in report
    assert "## Annual outcome distribution" in report
    if run.gate.passed:
        assert "**PASS**" in report
        assert "History is not a forecast" in report
        assert "| median |" in report
    else:
        assert "**FAIL**" in report
        assert "No distribution is derived for a failed strategy" in report
        for reason in run.gate.reasons:
            assert reason in report


def test_symbols_mode_default_run_unaffected(pg_session):
    """The ADR-0007 default path must not see validation instruments: a
    default run over the same DB state ranks ONLY the active universe."""
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2025, 6, 30, 22, tzinfo=UTC)))
    run = run_xsmom(s, audit, paths=5, seed=7)
    assert run.universe.included == ["SPY"]
    assert not set(ETFS) & set(run.universe.included)
    assert run.family == "xsmom" and run.top_n == 10
