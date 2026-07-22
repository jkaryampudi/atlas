"""Daily-cycle supervisor — dead-man / missed-cycle + stuck detection (F-025).

The single unsupervised in-process scheduler on one laptop missed cycles on
2026-07-16/19/20 with NO durable trace and NOTHING watching for the ABSENCE of a
completed cycle. This reader sits BESIDE the cycle (never inside its transaction)
and pages on absence:

  * MISSED — a business date past its expected-completion deadline with no
    attempted cycle_runs row (never even started). This is 'absence pages'.
  * STUCK — a 'running' row whose heartbeat/start is older than the stuck
    threshold (a crashed process that never wrote a terminal row); recovery
    rewrites it to 'killed' so the date is no longer blocked for a retry.

Every date is expected to run (the cycle executes a cheap calendar-aware no-op on
weekends/holidays too), so 'expected' is every calendar date whose 23:30-UTC fire
+ grace has passed — NOT is_trading_day. All time math takes an injected Clock
(invariant 6); the fire time mirrors scheduler.CYCLE_UTC (pinned by a test). Pages
go through alert_urgent's once-per-(kind,key) latch, so calling this every tick /
every hour is idempotent. A cold ledger (no rows yet) pages for nothing —
absence is only meaningful relative to the dates we have ever recorded.
"""
from __future__ import annotations

import os
import socket
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.core.db import session_scope
from atlas.ops.alerts import alert_urgent
from atlas.ops.cycle_ledger import (attempted_dates, earliest_business_date,
                                     mark_killed, running_cycles)

# Mirrors scheduler.CYCLE_UTC (pinned equal by test_scheduler_supervision_pg);
# defined here to avoid a scheduler<->supervise import cycle.
CYCLE_FIRE_UTC = time(23, 30)
# A cycle fires at 23:30 UTC; give it this long to finish before we call it
# missed (comfortably past the 00:30 UTC backup and any catch-up).
MISSED_GRACE_HOURS = 6
# A 'running' row whose heartbeat is older than this is not making progress ->
# PAGE (alive-but-hung or dead). It must exceed several HEARTBEAT_SECONDS.
STUCK_NO_PROGRESS_MIN = 15
# Absolute cap: a 'running' row older than this is recovered regardless of what
# the pid check says (cross-host, pid-reuse, or a truly wedged process). No real
# cycle runs anywhere near this long.
STUCK_ABSOLUTE_MAX_MIN = 720   # 12h
# How far back to scan for missed-cycle gaps. Bounds the scan while covering any
# realistic outage of the supervisor itself; the alert_urgent latch makes a wide
# window safe (each date pages at most once).
LOOKBACK_DAYS = 90


def cycle_deadline(business_date: date) -> datetime:
    """The instant by which business_date's cycle should have completed:
    D 23:30 UTC + grace."""
    return (datetime.combine(business_date, CYCLE_FIRE_UTC, tzinfo=UTC)
            + timedelta(hours=MISSED_GRACE_HOURS))


def expected_business_dates(clock: Clock, *, lookback_days: int = LOOKBACK_DAYS,
                            earliest: date | None = None) -> list[date]:
    """Every calendar date whose cycle deadline has passed, within the lookback
    window and not before the ledger's earliest recorded date."""
    now = clock.now().astimezone(UTC)
    today = now.date()
    start = today - timedelta(days=lookback_days)
    if earliest is not None and earliest > start:
        start = earliest
    out: list[date] = []
    d = start
    while d <= today:
        if cycle_deadline(d) < now:
            out.append(d)
        d += timedelta(days=1)
    return out


def check_missed_cycles(session: Session, clock: Clock, *,
                        lookback_days: int = LOOKBACK_DAYS) -> list[date]:
    """Page once (durable latch) for each expected date with no attempted cycle.
    Returns the dates that fired NOW. No-op on an empty ledger (cold-start)."""
    earliest = earliest_business_date(session)
    if earliest is None:
        return []
    expected = expected_business_dates(clock, lookback_days=lookback_days,
                                       earliest=earliest)
    attempted = attempted_dates(session, earliest)
    fired: list[date] = []
    for d in expected:
        if d in attempted:
            continue
        if alert_urgent(
                session, clock, kind="cycle_missed", key=d.isoformat(),
                title=f"Atlas: daily cycle MISSED for {d}",
                message=(f"No cycle_runs row exists for {d} and its "
                         f"{MISSED_GRACE_HOURS}h completion deadline "
                         f"({cycle_deadline(d):%Y-%m-%d %H:%M} UTC) has passed — "
                         "the scheduler never fired (laptop asleep / API down / "
                         "started without .env). The book was NOT processed; "
                         "run `make daily` and check the scheduler."),
                priority="high"):
            fired.append(d)
    return fired


def _process_alive(pid: int | None, host: str | None) -> bool | None:
    """Is the cycle's recorded process still alive? True/False when we can decide
    on THIS host via os.kill(pid, 0); None when we cannot (no pid, or the row was
    written on a different host — pid numbers are host-local). Single-host Atlas
    always takes the True/False branch."""
    if pid is None:
        return None
    if host is not None and host != socket.gethostname():
        return None                       # foreign host — pid not checkable here
    try:
        os.kill(pid, 0)
        return True                       # process exists
    except ProcessLookupError:
        return False                      # process is gone (crashed)
    except PermissionError:
        return True                       # exists, owned by another user
    except OSError:
        return None                       # undecidable


def check_stuck_cycles(session: Session, clock: Clock, *,
                       recover: bool = True) -> list[str]:
    """A 'running' row is recovered (killed) only when its process is provably
    DEAD on this host (a crash), or when it exceeds the absolute-max cap — so a
    legitimately long-running LIVE cycle is NEVER killed. A row whose process is
    alive but whose heartbeat has gone stale (hung, or a dead heartbeat thread)
    is PAGED but not auto-killed. Returns the cycle ids that fired a page NOW."""
    now = clock.now()
    no_progress = now - timedelta(minutes=STUCK_NO_PROGRESS_MIN)
    hard_cap = now - timedelta(minutes=STUCK_ABSOLUTE_MAX_MIN)
    fired: list[str] = []
    for row in running_cycles(session):
        cid, d = row.id, row.business_date
        last, started = row.last_seen, row.started_at
        alive = _process_alive(row.pid, row.host)
        dead = alive is False             # pid gone on this host -> crashed
        over_cap = started < hard_cap     # absurdly long -> recover regardless
        stale = last < no_progress        # heartbeat not advancing -> no progress
        if not (dead or over_cap or stale):
            continue                      # alive AND progressing (or too young)
        reason = ("process pid gone (crashed)" if dead
                  else "exceeded absolute max runtime" if over_cap
                  else f"no heartbeat since {last} (process alive but hung)")
        if alert_urgent(
                session, clock, kind="cycle_stuck", key=cid,
                title=f"Atlas: daily cycle STUCK for {d}",
                message=(f"Cycle {row.run_id} ({cid}) started {started} — "
                         f"{reason}. "
                         + ("Marking it killed so the date can be retried; "
                            "verify the book state." if (dead or over_cap)
                            else "Left running (alive); investigate — do NOT "
                                 "start a second cycle for this date.")),
                priority="high"):
            fired.append(cid)
        # Only RECOVER (free the date) when the process is truly gone or capped;
        # never kill a row whose process is still alive on the mere basis of a
        # stale heartbeat — that is the false-kill the review caught.
        if recover and (dead or over_cap):
            mark_killed(cid, clock=clock,
                        detail=f"stuck ({reason}), recovered by supervisor")
    return fired


def supervise(clock: Clock) -> dict[str, list[object]]:
    """Run both detectors in one autonomous session (the entry point wired into
    the scheduler tick and the hourly alerts sweep). Self-contained transaction
    so it survives regardless of any cycle's state."""
    with session_scope() as s:
        missed: list[object] = list(check_missed_cycles(s, clock))
        stuck: list[object] = list(check_stuck_cycles(s, clock))
    return {"missed": missed, "stuck": stuck}


def recover_on_startup(clock: Clock) -> list[str]:
    """Scheduler-boot recovery: a crash may have left a 'running' claim behind.
    check_stuck_cycles recovers it PROMPTLY because the recovery signal is the
    dead pid (not an elapsed-time threshold) — a crashed process's pid is gone,
    so the date is freed immediately for a retry; a genuinely-live concurrent run
    (from a CLI/launchd process) keeps an alive pid and is left untouched."""
    with session_scope() as s:
        return check_stuck_cycles(s, clock, recover=True)
