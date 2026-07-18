"""xsmom_run integration smoke on atlas_test: seeded vendor-shaped fixture
bars through the FULL runner path — panel load with exclusion counting (holey
series and non-US calendars are excluded and counted, never silently dropped),
ONE registered trial in family 'xsmom', monkey null, both benchmarks, purged
walk-forward, the audit event, and the report text with the survivorship
caveat and verbatim verdict.

Robust to shared-DB state by construction: every price bar in the test DB is
deleted INSIDE the test transaction (rolled back at teardown), so the panel
sees exactly this test's instruments and nothing another suite committed."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.registry import trial_count
from atlas.dcp.backtest.xsmom_run import render_report, run_xsmom
from atlas.dcp.market_data.calendars import trading_days_between
from tests.conftest import requires_pg

pytestmark = requires_pg

SESSIONS = trading_days_between("US", date(2024, 1, 2), date(2025, 6, 30))
STOCKS = [f"XSM{k:02d}" for k in range(11)]     # + SPY -> 12 aligned US names


def _instrument(s, symbol: str, market: str = "US") -> str:
    existing = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = :sym"),
        {"sym": symbol}).scalar()
    if existing is not None:
        return str(existing)
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', :m, 'stock', :sym, 'Information Technology', "
        "'USD') RETURNING id"), {"sym": symbol, "m": market}).scalar())


def _bars(s, iid: str, dates: list[date], rate: float) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, 1000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": round(100.0 * math.exp(rate * i), 6)}
         for i, d in enumerate(dates)])


def _seed(s) -> None:
    s.execute(text("DELETE FROM market.price_bars_daily"))       # in-txn only
    # lineage-scoped isolation (ADR-0016): the runner deflates at the
    # momentum LINEAGE count, so leftovers from committed tests in ANY
    # momentum-lineage family would inflate n_trials here
    s.execute(text("DELETE FROM quant.trial_registry WHERE lineage = 'momentum'"))
    _bars(s, _instrument(s, "SPY"), SESSIONS, 0.0004)
    for k, sym in enumerate(STOCKS):                # distinct drifts -> ranking
        _bars(s, _instrument(s, sym), SESSIONS, -0.001 + 0.0003 * k)
    # holey series: one session missing mid-history -> excluded, counted
    holey = SESSIONS[:150] + SESSIONS[151:]
    _bars(s, _instrument(s, "XSHOLE"), holey, 0.0005)
    # non-US calendar -> excluded before any completeness check
    _bars(s, _instrument(s, "XSAU", market="AU"), SESSIONS, 0.0005)


def test_full_runner_path_smoke(pg_session):
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2025, 6, 30, 22, tzinfo=UTC)))

    run = run_xsmom(s, audit, paths=15, seed=7)

    # universe honesty: 12 included, 2 excluded with counted reasons
    assert run.universe.included == sorted(["SPY", *STOCKS])
    reasons = {e.symbol: e.reason for e in run.universe.excluded}
    assert set(reasons) == {"XSHOLE", "XSAU"}
    assert "missing session" in reasons["XSHOLE"]
    assert "non-US session calendar" in reasons["XSAU"]

    # ONE trial, family xsmom, true count feeds the gate
    assert trial_count(s, "xsmom") == 1
    assert run.n_trials == 1
    assert run.trials_after_total == run.trials_before_total + 1
    assert run.gate.n_trials == 1

    # engine actually traded and the gate compared both benchmarks
    assert run.result.n_rebalances >= 3
    assert run.start == run.universe.panel.dates[252]
    assert 0.0 <= run.gate.null_p_value <= 1.0
    assert run.gate.spy_bh_return != run.gate.ew_return
    assert len(run.wf.fold_results) == 4

    # audit trail: quant.backtest.completed with the verdict payload
    row = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'quant.backtest.completed' "
        "AND entity_id = 'xsmom/portfolio' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert row is not None
    assert row["gate_passed"] == run.gate.passed
    assert row["n_trials"] == 1
    assert "survivorship" in row["survivorship_caveat"]

    # report: survivorship caveat prominent, verdict verbatim, both benchmarks
    report = render_report(run, paths=15)
    assert "SURVIVORSHIP BIAS CAVEAT" in report
    assert "Gate verdict" in report
    assert "BINDING benchmark" in report
    assert "equal-weight all-eligible" in report
    assert "XSHOLE" in report and "XSAU" in report
    if run.gate.passed:
        assert "pending point-in-time constituent validation" in report
    else:
        assert "FAIL" in report
        for reason in run.gate.reasons:
            assert reason in report


def test_runner_is_deterministic_for_a_seed(pg_session):
    """Same panel, same seed -> identical verdict inputs (the second call
    registers a second trial, so only seed-driven fields are compared)."""
    s = pg_session
    _seed(s)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2025, 6, 30, 22, tzinfo=UTC)))
    a = run_xsmom(s, audit, paths=10, seed=7)
    b = run_xsmom(s, audit, paths=10, seed=7)
    assert a.result.total_return == b.result.total_return
    assert a.gate.null_p_value == b.gate.null_p_value
    assert a.gate.spy_bh_return == b.gate.spy_bh_return
    assert a.gate.ew_return == b.gate.ew_return
    assert b.n_trials == 2                       # honesty: every run counts
    assert b.gate.dsr <= a.gate.dsr              # more trials never inflate DSR
