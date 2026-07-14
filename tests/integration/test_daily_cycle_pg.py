"""T0-T9 daily cycle orchestration (atlas/ops/daily.py): the go-live loop.

Verifies ORDERING POLICY and failure semantics, not the per-step logic (each
step has its own suite): gates land before trading, stops fire before
settlement, the snapshot marks the post-fill book, reconciliation guards the
lot ledger, and an ingest failure pages but never stops the book from being
protected. Seeding mirrors test_exits_pg.py exactly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.proposals import approve, build_proposal
from atlas.ops.daily import EXIT_REFUSED, CycleRefusedError, run_daily_cycle
from tests.conftest import URL as TEST_DB_URL
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
FX_USD_AUD = Decimal("1.5")


def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots",
              "trading.reconciliations"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM workflow.workflow_node_results "
                   "WHERE run_id LIKE 'daily-%'"))
    s.execute(text("DELETE FROM workflow.workflow_runs WHERE run_id LIKE 'daily-%'"))
    # ADR-0010 wiring debris from the signal/band suites: without this, a
    # leftover paper strategy row makes t5b/t6b active and the exact node
    # strings below drift
    s.execute(text("DELETE FROM quant.sleeve_daily"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies WHERE family = 'xsmom-pit-tr'"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZCY%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZCY%'"))


def _seed_entered_position(s, clock) -> str:
    """A held ZCYA position with a 95 stop, entered via the real lifecycle
    (build -> approve -> fill at 2026-07-14 open 102)."""
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) VALUES "
        "('ZCYA', 'XTEST', 'US', 'stock', 'ZCYA', 'Information Technology', 'USD') "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "VALUES (:iid, :d, 100, 101, 99, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)} for i in range(21)])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-10',:r,'zcy-test'), "
        "       ('USD','AUD','2026-07-14',:r,'zcy-test'), "
        "       ('USD','AUD','2026-07-15',:r,'zcy-test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": FX_USD_AUD})
    # created_at from the injected clock: the bridge's 48h candidacy window
    # must never depend on the DB wall clock (deterministic replays)
    memo_id = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        "evidence_refs, created_at) VALUES ('committee', 'ZCYA', 'BUY', '[]', :ca) "
        "RETURNING id"), {"ca": clock.now()}).scalar())
    res = build_proposal(s, clock, memo_id=memo_id, symbol="ZCYA",
                         signal_refs=[str(uuid4())], entry_price=Decimal("100"),
                         stop_price=Decimal("95"), target_price=Decimal("120"))
    assert res.state == "pending_approval"
    clock.advance_to(T0 + timedelta(hours=1))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"
    # entry fill session 2026-07-14 open 102; then 7/15 bar breaches the stop
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) VALUES "
        "(:iid, '2026-07-14', 102, 104, 101, 103, 1000000, 'EodhdAdapter'), "
        "(:iid, '2026-07-15', 96, 97, 94, 94.5, 1000000, 'EodhdAdapter')"),
        {"iid": iid})
    return str(iid)


def test_full_cycle_ordering_and_kill_guards(clean_audit):
    s = clean_audit
    _clean(s)
    clock = FrozenClock(T0)
    _seed_entered_position(s, clock)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))

    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert list(results.keys()) == [
        "t0_ingest", "t1_verify_chain", "t2_expire", "t3_settle", "t4_stops",
        "t5_snapshot", "t5b_bands", "t6_reconcile", "t6b_signals", "t7_desk",
        "t8_bridge", "t9_report"]
    # no approved strategy in this fixture: both ADR-0010 nodes idle honestly
    assert results["t5b_bands"] == "bands idle (no banded strategy)"
    assert results["t6b_signals"] == ("signals idle (no paper/live "
                                      "xsmom-pit-tr strategy)")
    # the entry order fills at the 7/14 open AND the 7/15 bar fires the stop
    # in ONE cycle: settle runs before the scan precisely so a just-entered
    # position is never naked while its breaching bar is already ingested
    assert results["t3_settle"] == "fills=1"
    assert results["t4_stops"] == "stops_fired=1"
    assert results["t6_reconcile"] == "clean"
    assert results["t1_verify_chain"].startswith("chain ok (")
    assert results["t7_desk"] == "desk off (no model key configured)"
    # the seeded memo (2026-07-13 20:00) is >48h old at this cycle's clock:
    # a stale thesis is not a bridge candidate at all
    assert results["t8_bridge"] == "bridged 0 (none) · skipped 0"

    pos = s.execute(text(
        "SELECT qty, closed_at FROM trading.positions")).one()
    assert pos.qty == 0 and pos.closed_at is not None     # stopped out
    recon = s.execute(text(
        "SELECT status, diffs FROM trading.reconciliations")).one()
    assert recon.status == "clean" and recon.diffs == []
    nav = s.execute(text(
        "SELECT nav_aud FROM trading.portfolio_snapshots")).scalar()
    assert nav is not None                                # book marked post-fill
    evs = [r[0] for r in s.execute(text(
        "SELECT event_type FROM audit.decision_events ORDER BY seq")).all()]
    assert "daily_cycle.completed" in evs
    assert "reconciliation.completed" in evs

    # idempotent same-day re-run: every node skips, nothing double-fires
    again = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert again["t4_stops"] == "stops_fired=1"           # cached, not re-run
    n_ex = s.execute(text("SELECT count(*) FROM trading.executions")).scalar()
    assert n_ex == 2                                      # entry + stop, no more
    n_snap = s.execute(text(
        "SELECT count(*) FROM trading.portfolio_snapshots")).scalar()
    assert n_snap == 1


def test_reconciliation_break_kills_the_run(clean_audit):
    s = clean_audit
    _clean(s)
    clock = FrozenClock(T0)
    _seed_entered_position(s, clock)
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))  # entry fill only
    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert results["t6_reconcile"] == "clean"

    # corrupt the lot ledger behind the lifecycle's back
    s.execute(text("UPDATE trading.tax_lots SET qty = qty + 1"))
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))
    with pytest.raises(RuntimeError, match="reconciliation BREAK"):
        run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    row = s.execute(text(
        "SELECT status FROM trading.reconciliations ORDER BY created_at DESC "
        "LIMIT 1")).scalar()
    assert row == "break"


def test_desk_failure_pages_but_never_undoes_trading(clean_audit):
    """The desk runs AFTER settlement and reconciliation: a model-side
    catastrophe flags the run for the operator, but every trading step's
    work stands — agents can never take the book down with them."""
    s = clean_audit
    _clean(s)
    clock = FrozenClock(T0)
    _seed_entered_position(s, clock)
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))

    def exploding_desk(session, clk):
        raise RuntimeError("model API melted")

    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES),
                              desk=exploding_desk)
    assert results["t3_settle"] == "fills=1"              # trading unaffected
    assert results["t6_reconcile"] == "clean"
    assert results["t7_desk"].startswith("desk FAILED: model API melted")
    # the bridge still ran after the desk failure: the memo is fresh at this
    # clock but already consumed by the entry proposal -> recorded skip
    assert results["t8_bridge"] == "bridged 0 (none) · skipped 1"
    assert "desk FAILED" in results["t9_report"]
    n_ex = s.execute(text("SELECT count(*) FROM trading.executions")).scalar()
    assert n_ex == 1


def test_desk_skipped_on_non_us_session_day(clean_audit):
    """2026-07-12 is a Sunday in UTC: the desk has nothing new to read, so it
    must not spend a cent — and must say so."""
    s = clean_audit
    _clean(s)
    clock = FrozenClock(datetime(2026, 7, 12, 23, 30, tzinfo=UTC))
    calls = {"n": 0}

    def counting_desk(session, clk):
        calls["n"] += 1

    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES),
                              desk=counting_desk)
    assert results["t7_desk"] == "desk skipped (2026-07-12 is not a US session)"
    assert calls["n"] == 0
    # the bridge is NOT desk-gated: it runs (and finds no candidates) even on
    # a non-session day — a manually-run desk's memos must still bridge
    assert results["t8_bridge"] == "bridged 0 (none) · skipped 0"


# ---- session-close guard (production defect 2026-07-13): a mid-session
# console click must never consume the day's one-per-date checkpoint --------

REFUSED_MSG = ("cycle for 2026-07-13 refused: US session not yet closed "
               "(closes 20:00 UTC + 30min vendor grace); re-run after 20:30 UTC")
NODES = ["t0_ingest", "t1_verify_chain", "t2_expire", "t3_settle", "t4_stops",
         "t5_snapshot", "t5b_bands", "t6_reconcile", "t6b_signals", "t7_desk",
         "t8_bridge", "t9_report"]


def test_mid_session_start_is_refused_before_the_checkpoint_exists(
        clean_audit, capsys):
    """The defect instant (11:07 AEST = 01:07 UTC, Monday 2026-07-13): the
    guard refuses BEFORE the daily-2026-07-13 checkpoint row is created —
    no workflow rows, no audit event, nothing written anywhere."""
    s = clean_audit
    _clean(s)
    clock = FrozenClock(datetime(2026, 7, 13, 1, 7, tzinfo=UTC))
    with pytest.raises(CycleRefusedError) as exc:
        run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert str(exc.value) == REFUSED_MSG
    assert s.execute(text("SELECT count(*) FROM workflow.workflow_runs "
                          "WHERE run_id = 'daily-2026-07-13'")).scalar() == 0
    assert s.execute(text("SELECT count(*) FROM workflow.workflow_node_results "
                          "WHERE run_id = 'daily-2026-07-13'")).scalar() == 0
    # the guard is a pure read of calendar + clock: no audit event either
    assert s.execute(text(
        "SELECT count(*) FROM audit.decision_events")).scalar() == 0
    # the refusal rides the @@CYCLE stream so the console shows WHY
    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if ln.startswith("@@CYCLE ")]
    ev = json.loads(lines[-1][8:])
    assert (ev["node"], ev["status"], ev["result"]) == (
        "guard", "refused", REFUSED_MSG)


def test_refused_attempt_then_after_close_run_is_fresh_and_complete(clean_audit):
    """A refusal must not spend the day: the post-close invocation is a FRESH
    full run — every node truly executes (skipped nodes emit no
    workflow.node.completed audit event, so 12 events = nothing was
    pre-consumed) and the day's checkpoint completes."""
    s = clean_audit
    _clean(s)
    clock = FrozenClock(datetime(2026, 7, 13, 1, 7, tzinfo=UTC))
    with pytest.raises(CycleRefusedError):
        run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    clock.advance_to(datetime(2026, 7, 13, 22, 0, tzinfo=UTC))  # close+grace passed
    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert list(results.keys()) == NODES
    assert s.execute(text("SELECT status FROM workflow.workflow_runs "
                          "WHERE run_id = 'daily-2026-07-13'")).scalar() == "completed"
    executed = s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'workflow.node.completed'")).scalar()
    assert executed == len(NODES)


def test_scheduled_2330_utc_firing_runs_the_day(clean_audit):
    """The scheduler's own 23:30 UTC firing time passes the guard trivially
    for its target date (the structural property is pinned across a whole
    year in tests/unit/test_cycle_guard.py)."""
    s = clean_audit
    _clean(s)
    clock = FrozenClock(datetime(2026, 7, 13, 23, 30, tzinfo=UTC))
    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert list(results.keys()) == NODES
    assert s.execute(text("SELECT status FROM workflow.workflow_runs "
                          "WHERE run_id = 'daily-2026-07-13'")).scalar() == "completed"


def test_weekend_mid_morning_keeps_not_a_session_behavior(clean_audit):
    """The guard only speaks on US session dates: a Sunday run at the exact
    mid-morning instant of the defect still runs the whole cycle and records
    the existing 'not a US session' desk line — no regression."""
    s = clean_audit
    _clean(s)
    clock = FrozenClock(datetime(2026, 7, 12, 1, 7, tzinfo=UTC))
    calls = {"n": 0}

    def counting_desk(session, clk):
        calls["n"] += 1

    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES),
                              desk=counting_desk)
    assert results["t7_desk"] == "desk skipped (2026-07-12 is not a US session)"
    assert calls["n"] == 0


def test_cli_exits_with_the_distinct_refused_code(clean_audit):
    """The scheduler's envelope, end to end: `python -m atlas.ops.daily`
    mid-session exits EXIT_REFUSED (not 0 = clean, not 2 = failure), prints
    the guard's @@CYCLE line for the console and the plain REFUSED line for
    the status detail — and still creates no checkpoint row."""
    s = clean_audit
    _clean(s)
    env = {**os.environ,
           "ATLAS_DATABASE_URL": TEST_DB_URL,   # isolation, defense in depth:
           #                        the guard fires before any DB statement
           "ATLAS_EODHD_API_KEY": "",           # fixtures — never a vendor call
           "ATLAS_ANTHROPIC_API_KEY": ""}       # desk off
    r = subprocess.run([sys.executable, "-m", "atlas.ops.daily",
                        "--now", "2026-07-13T01:07:00+00:00"],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == EXIT_REFUSED, r.stdout + r.stderr
    assert f"REFUSED: {REFUSED_MSG}" in r.stdout
    guard_lines = [ln for ln in r.stdout.splitlines() if ln.startswith("@@CYCLE ")]
    ev = json.loads(guard_lines[-1][8:])
    assert (ev["node"], ev["status"], ev["result"]) == (
        "guard", "refused", REFUSED_MSG)
    assert s.execute(text("SELECT count(*) FROM workflow.workflow_runs "
                          "WHERE run_id = 'daily-2026-07-13'")).scalar() == 0
