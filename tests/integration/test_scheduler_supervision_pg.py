"""F-025 / M56 / M57 — durable daily-cycle ledger + dead-man supervision.

The ledger writes on a connection SEPARATE from the cycle's atomic transaction,
so a FAILED cycle leaves a durable row + captured LLM spend (M56/M57) even though
its own workflow_runs/audit/spend rows roll back. The supervisor pages on the
ABSENCE of a completed cycle (dead-man) and on stuck 'running' rows.

The ledger opens its OWN session_scope() (autonomous by design), so these tests
repoint the app engine at atlas_test and ASSERT current_database() before writing
— a misconfigured run fails loud rather than touching production."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.ops import scheduler, supervise
from atlas.ops.cycle_ledger import (CycleOverlapError, close_cycle,
                                    day_llm_spend, open_cycle)
from atlas.ops.supervise import (check_missed_cycles, check_stuck_cycles,
                                 recover_on_startup)
from tests.conftest import (TEST_DB_NAME, URL, _ensure_test_db, requires_pg,
                            reset_app_engine)

pytestmark = requires_pg

URGENT_EVENT = "ops.alert.urgent"


@pytest.fixture
def ledger_db(monkeypatch):
    """App engine repointed at atlas_test (the ledger's autonomous session_scope
    must never hit prod). Truncates the ledger + audit + spend so each test is
    isolated (autonomous commits survive a plain session rollback)."""
    _ensure_test_db()
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    from atlas.core.db import _factory, session_scope
    with session_scope() as s:
        assert s.execute(text("SELECT current_database()")).scalar() == TEST_DB_NAME, \
            "app engine is NOT on atlas_test — refusing to run (prod-write guard)"
        s.execute(text("TRUNCATE ops.cycle_runs, audit.decision_events, "
                       "audit.chain_head, research.agent_runs RESTART IDENTITY CASCADE"))
    q = _factory()()          # app-bound (atlas_test) session for READ-COMMITTED reads
    yield q
    q.close()
    reset_app_engine()


THIS_HOST = socket.gethostname()


def _dead_pid() -> int:
    """A pid that is guaranteed NOT alive (spawn a trivial process and reap it)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _at(y, m, d, hh=23, mm=45) -> FrozenClock:
    return FrozenClock(datetime(y, m, d, hh, mm, tzinfo=UTC))


def _complete(bday: date, *, status="completed", exit_code=0) -> str:
    clk = FrozenClock(datetime.combine(bday, time(23, 45), tzinfo=UTC))
    cid = open_cycle(bday, f"daily-{bday}", "scheduler", clock=clk)
    close_cycle(cid, status, clock=clk, exit_code=exit_code)
    return cid


# --------------------------------------------------------------------------- #
def test_supervise_fire_time_pinned_to_scheduler():
    """The dead-man's expected-fire time must not drift from the scheduler's."""
    assert supervise.CYCLE_FIRE_UTC == scheduler.CYCLE_UTC == time(23, 30)


def test_open_close_records_lifecycle(ledger_db):
    q = ledger_db
    clk = _at(2026, 7, 20)
    cid = open_cycle(date(2026, 7, 20), "daily-2026-07-20", "cli", clock=clk)
    assert q.execute(text("SELECT status FROM ops.cycle_runs WHERE id=:i"),
                     {"i": cid}).scalar() == "running"
    close_cycle(cid, "completed", clock=clk, exit_code=0, llm_spend=Decimal("1.25"))
    row = q.execute(text("SELECT status, exit_code, llm_spend_usd, finished_at "
                         "FROM ops.cycle_runs WHERE id=:i"), {"i": cid}).one()
    assert (row.status, row.exit_code, row.llm_spend_usd) == \
        ("completed", 0, Decimal("1.2500"))
    assert row.finished_at is not None


def test_overlap_second_running_is_refused(ledger_db):
    clk = _at(2026, 7, 20)
    open_cycle(date(2026, 7, 20), "daily-2026-07-20", "scheduler", clock=clk)
    with pytest.raises(CycleOverlapError):
        open_cycle(date(2026, 7, 20), "daily-2026-07-20", "cli", clock=clk)


def test_failed_cycle_persists_row_and_spend_across_rollback(ledger_db):
    """M56 + M57: a hard failure rolls back the cycle's own writes (spend rows),
    but the ledger row + the spend captured before rollback survive."""
    q = ledger_db
    from atlas.core.db import session_scope
    clk = _at(2026, 7, 20)
    bday = date(2026, 7, 20)
    cid = open_cycle(bday, "daily-2026-07-20", "scheduler", clock=clk)
    spend = Decimal(0)
    with pytest.raises(RuntimeError):
        with session_scope() as cyc:
            try:
                cyc.execute(text(
                    "INSERT INTO research.agent_runs (agent_role, "
                    " prompt_template_hash, model, status, cost_usd, created_at) "
                    "VALUES ('cio','h','claude-x','ok',:c,:t)"),
                    {"c": Decimal("2.50"), "t": clk.now()})
                raise RuntimeError("reconciliation BREAK — lot ledger disagrees")
            finally:
                spend = day_llm_spend(cyc, bday)      # sees the uncommitted 2.50
    close_cycle(cid, "failed", clock=clk, exit_code=1, llm_spend=spend,
                detail="reconciliation BREAK — lot ledger disagrees")

    # the cycle's OWN writes rolled back (invisible to a fresh read):
    assert q.execute(text("SELECT count(*) FROM research.agent_runs "
                          "WHERE created_at::date=:d"), {"d": bday}).scalar() == 0
    # the durable ledger row survived, with the captured spend + reason:
    row = q.execute(text("SELECT status, exit_code, llm_spend_usd, detail "
                         "FROM ops.cycle_runs WHERE id=:i"), {"i": cid}).one()
    assert (row.status, row.exit_code) == ("failed", 1)
    assert row.llm_spend_usd == Decimal("2.5000")     # M57: spend not undercounted
    assert "BREAK" in row.detail                       # M56: reason durable


def test_missed_cycle_dead_man_pages_once(ledger_db):
    q = ledger_db
    _complete(date(2026, 7, 15))                       # earliest recorded
    _complete(date(2026, 7, 18))
    clk = FrozenClock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    missed = check_missed_cycles(q, clk)
    # expected [07-15..07-19] (07-20 deadline not yet passed); attempted {07-15,07-18}
    assert set(missed) == {date(2026, 7, 16), date(2026, 7, 17), date(2026, 7, 19)}
    # idempotent: the alert_urgent latch means a second sweep pages nothing
    assert check_missed_cycles(q, clk) == []
    n = q.execute(text("SELECT count(*) FROM audit.decision_events "
                       "WHERE event_type=:e"), {"e": URGENT_EVENT}).scalar()
    assert n == 3


def test_failed_cycle_is_not_flagged_missed(ledger_db):
    """A cycle that ran and FAILED was attempted (its failure pages elsewhere) —
    the dead-man must not double-page it as 'never ran'."""
    q = ledger_db
    _complete(date(2026, 7, 15))
    _complete(date(2026, 7, 16), status="failed", exit_code=1)
    clk = FrozenClock(datetime(2026, 7, 18, 12, 0, tzinfo=UTC))
    missed = check_missed_cycles(q, clk)
    assert date(2026, 7, 16) not in missed            # attempted (failed) != missed
    assert date(2026, 7, 17) in missed                 # genuinely absent


def test_cold_ledger_does_not_false_page(ledger_db):
    """An empty ledger has no anchor — absence is only meaningful relative to
    dates we have ever recorded, so nothing pages."""
    clk = FrozenClock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    assert check_missed_cycles(ledger_db, clk) == []


def test_weekend_dates_are_expected_not_skipped(ledger_db):
    """'Expected' is every calendar day past its deadline (the cycle runs a cheap
    no-op on weekends), NOT is_trading_day — a weekend gap must still page."""
    q = ledger_db
    assert date(2026, 7, 18).weekday() >= 5           # confirm Saturday/Sunday
    _complete(date(2026, 7, 17))                       # Friday anchor
    clk = FrozenClock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    missed = check_missed_cycles(q, clk)
    assert date(2026, 7, 18) in missed                 # weekend gap flagged


def test_crashed_cycle_recovered_and_date_freed(ledger_db):
    """A CRASHED cycle (its recorded pid is gone) is killed + the date freed."""
    q = ledger_db
    started = FrozenClock(datetime(2026, 7, 20, 10, 0, tzinfo=UTC))
    cid = open_cycle(date(2026, 7, 20), "daily-2026-07-20", "scheduler",
                     clock=started, host=THIS_HOST, pid=_dead_pid())
    now = FrozenClock(datetime(2026, 7, 20, 14, 30, tzinfo=UTC))
    fired = check_stuck_cycles(q, now, recover=True)
    assert cid in fired
    assert q.execute(text("SELECT status FROM ops.cycle_runs WHERE id=:i"),
                     {"i": cid}).scalar() == "killed"
    # recovery freed the date: a retry can now claim it (overlap guard cleared)
    cid2 = open_cycle(date(2026, 7, 20), "daily-2026-07-20", "cli", clock=now,
                      host=THIS_HOST, pid=os.getpid())
    assert cid2 != cid
    assert check_stuck_cycles(q, now, recover=True) == []   # cid2 is alive+fresh


def test_live_cycle_is_never_killed(ledger_db):
    """The false-kill the review caught: a legitimately LONG-running cycle
    (alive pid) must never be killed, even long after start. A stale heartbeat
    on a live process PAGES (hung) but does NOT release the overlap claim."""
    q = ledger_db
    started = FrozenClock(datetime(2026, 7, 20, 10, 0, tzinfo=UTC))
    cid = open_cycle(date(2026, 7, 20), "daily-2026-07-20", "scheduler",
                     clock=started, host=THIS_HOST, pid=os.getpid())  # alive
    # 4.5h later, heartbeat frozen at start -> hung signal, but pid is ALIVE
    now = FrozenClock(datetime(2026, 7, 20, 14, 30, tzinfo=UTC))
    fired = check_stuck_cycles(q, now, recover=True)
    assert cid in fired                                   # PAGED (hung)
    assert q.execute(text("SELECT status FROM ops.cycle_runs WHERE id=:i"),
                     {"i": cid}).scalar() == "running"    # NOT killed — still claims the date
    # so a second cycle for the same date is still refused (guard intact)
    with pytest.raises(Exception):
        open_cycle(date(2026, 7, 20), "daily-2026-07-20", "cli", clock=now,
                   host=THIS_HOST, pid=os.getpid())


def test_fresh_live_cycle_is_not_even_paged(ledger_db):
    """A healthy cycle (alive pid, recent heartbeat) triggers nothing."""
    q = ledger_db
    now = FrozenClock(datetime(2026, 7, 20, 23, 45, tzinfo=UTC))
    open_cycle(date(2026, 7, 20), "daily-2026-07-20", "scheduler", clock=now,
               host=THIS_HOST, pid=os.getpid())           # heartbeat = now
    assert check_stuck_cycles(q, now, recover=True) == []


def test_recover_on_startup_clears_recent_crash(ledger_db):
    """Fix for the review's recover-threshold bug: a crash newer than the old
    180-min window (crash 23:30, restart 23:50) is cleared PROMPTLY, because the
    signal is the dead pid — not an elapsed-time threshold."""
    q = ledger_db
    started = FrozenClock(datetime(2026, 7, 20, 23, 30, tzinfo=UTC))
    open_cycle(date(2026, 7, 20), "daily-2026-07-20", "scheduler", clock=started,
               host=THIS_HOST, pid=_dead_pid())           # crashed
    boot = FrozenClock(datetime(2026, 7, 20, 23, 50, tzinfo=UTC))   # +20min
    # recover_on_startup uses the app engine, already repointed to atlas_test by
    # the ledger_db fixture.
    fired = recover_on_startup(boot)
    assert len(fired) == 1
    assert q.execute(text("SELECT count(*) FROM ops.cycle_runs "
                          "WHERE business_date=:d AND status='running'"),
                     {"d": date(2026, 7, 20)}).scalar() == 0   # date freed


def test_missed_lookback_covers_gaps_older_than_a_week(ledger_db):
    """Fix for the review's lookback bug: a gap 8+ days old (beyond the old
    7-day window) is still flagged, not silently skipped."""
    q = ledger_db
    _complete(date(2026, 7, 1))                            # earliest anchor
    _complete(date(2026, 7, 3))
    # today far enough that 07-02 is >7 days old
    clk = FrozenClock(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
    missed = check_missed_cycles(q, clk)
    assert date(2026, 7, 2) in missed                      # 13 days old — still caught
