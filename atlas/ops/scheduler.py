"""In-process scheduler + one-click cycle trigger — the console is the ONLY
control surface (the Principal never needs a terminal).

Enabled with ATLAS_INPROC_SCHEDULER=1 (the Mac interim, where launchd is
blocked by TCC on ~/Documents). On the Linux box, systemd timers own the
schedule and this stays OFF — never both, or the day fires twice (harmless —
the run_id is per-day and idempotent — but noisy).

Fires the T0-T9 cycle at 23:30 UTC (09:30 AEST) and the backup at 00:30 UTC,
exactly like the launchd/systemd schedules. The manual trigger (POST
/v1/system/run-daily) runs the SAME cycle in a worker thread; a non-blocking
lock refuses a second concurrent run ("already running" is an answer, not an
error). Wall clock here is legitimate: this is the ops layer deciding WHEN to
run; the cycle itself still receives an injectable Clock.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from atlas.ops.alerts import notify

CYCLE_UTC = time(23, 30)     # 09:30 AEST — after the US close + EODHD publish
BACKUP_UTC = time(0, 30)     # an hour later, so the dump includes the run
_REPO = Path(__file__).resolve().parents[2]

_cycle_lock = threading.Lock()
_last: dict[str, object] = {"started_at": None, "finished_at": None,
                            "ok": None, "refused": None, "detail": None}


def next_fire(now: datetime, at: time) -> datetime:
    """The next UTC instant of the daily `at` time strictly after `now`."""
    if now.tzinfo is None:
        raise ValueError("next_fire requires an aware datetime")
    now = now.astimezone(UTC)
    candidate = datetime.combine(now.date(), at, tzinfo=UTC)
    return candidate if candidate > now else candidate + timedelta(days=1)


def _run_cycle() -> None:
    """One full cycle via the CLI entrypoint in a subprocess: crash isolation
    (a segfaulting cycle must not take the API down) and an honest exit code.
    The subprocess prints one @@CYCLE json line per node transition (the run
    is a single uncommitted transaction, so this stream is the ONLY live
    window into it); each line lands in _last['progress'] for the console's
    animated pipeline. The pipeline does its own alerting; this reports only
    the envelope.

    EXIT_REFUSED is the one non-zero code that is NOT a failure: the cycle
    politely declined to start because the date's US session has not closed
    yet (atlas.ops.daily session-close guard — the WHY arrives as a @@CYCLE
    'guard'/'refused' progress line and in the detail tail). No checkpoint
    row was created, the day is not consumed, and nobody gets paged; the
    scheduled 23:30 UTC firing always passes the guard, so a refusal can only
    come from a manual click landing before the close."""
    import json as _json

    from atlas.ops.daily import EXIT_REFUSED  # lazy: keep module import light

    _last.update(started_at=datetime.now(UTC).isoformat(), finished_at=None,
                 ok=None, refused=None, detail="running", progress=[])
    progress: list[dict[str, object]] = _last["progress"]  # type: ignore[assignment]
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "atlas.ops.daily"], cwd=_REPO,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1)
        tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("@@CYCLE "):
                try:
                    ev = _json.loads(line[8:])
                except ValueError:
                    continue
                # one entry per node: 'running' appends, 'done'/'failed' updates
                for entry in progress:
                    if entry.get("node") == ev.get("node"):
                        entry.update(ev)
                        break
                else:
                    progress.append(ev)
            else:
                tail.append(line)
                del tail[:-30]
        proc.wait(timeout=3600)
        refused = proc.returncode == EXIT_REFUSED
        ok = proc.returncode == 0 or refused
        detail = "\n".join(tail)[-500:]
        if not ok:
            notify("Atlas daily cycle FAILED",
                   f"exit {proc.returncode} — console jobs board has the step",
                   priority="high")
    except Exception as e:  # noqa: BLE001 — the scheduler must survive anything
        ok, refused, detail = False, False, f"cycle runner crashed: {e}"
        notify("Atlas daily cycle CRASHED", str(e), priority="high")
    _last.update(finished_at=datetime.now(UTC).isoformat(), ok=ok,
                 refused=refused, detail=detail)


def start_cycle() -> bool:
    """Manual/scheduled trigger. Returns False when a cycle is already
    running (the caller reports 'busy', nothing fires twice)."""
    if not _cycle_lock.acquire(blocking=False):
        return False

    def _target() -> None:
        try:
            _run_cycle()
        finally:
            _cycle_lock.release()

    threading.Thread(target=_target, name="atlas-cycle", daemon=True).start()
    return True


def status() -> dict[str, object]:
    now = datetime.now(UTC)
    last = dict(_last)
    last["progress"] = [dict(e) for e in _last.get("progress", [])]  # type: ignore[union-attr]
    return {"cycle_running": _cycle_lock.locked(),
            "next_cycle_utc": next_fire(now, CYCLE_UTC).isoformat(),
            "next_backup_utc": next_fire(now, BACKUP_UTC).isoformat(),
            "last": last}


def _run_backup() -> None:
    script = _REPO / "ops" / ("backup.sh" if sys.platform == "darwin"
                              else "backup_linux.sh")
    try:
        proc = subprocess.run(["/bin/bash", str(script)], cwd=_REPO,
                              capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            notify("Atlas backup FAILED", (proc.stderr or "")[-300:],
                   priority="high")
    except Exception as e:  # noqa: BLE001
        notify("Atlas backup CRASHED", str(e), priority="high")


# Short wall-clock tick. asyncio.sleep counts MONOTONIC time, which pauses
# while a laptop lid is closed — a single long sleep to a fire time silently
# owes every slept hour on wake (observed live: the 2026-07-15 cycle fired 16
# minutes late, 2026-07-16's never fired before a restart re-armed it for the
# next day, and 2026-07-17's was skipped outright). Ticking every TICK_SECONDS
# against the WALL clock survives system sleep: on wake the very next tick
# notices the fire time has passed and catches up. Re-fires are safe by
# construction — the daily checkpoint replays an already-completed date and
# the pre-session guard refuses a too-early one.
TICK_SECONDS = 30.0


async def scheduler_loop() -> None:
    """Wall-clock tick loop for the two daily fire times. Started from the
    API's lifespan when ATLAS_INPROC_SCHEDULER=1. Each pending fire time is
    executed as soon as a tick observes it in the past (catch-up after system
    sleep), then re-armed for the next day."""
    now = datetime.now(UTC)
    pending = {"cycle": next_fire(now, CYCLE_UTC),
               "backup": next_fire(now, BACKUP_UTC)}
    while True:
        await asyncio.sleep(TICK_SECONDS)
        now = datetime.now(UTC)
        if now >= pending["cycle"]:
            pending["cycle"] = next_fire(now, CYCLE_UTC)
            if not start_cycle():
                notify("Atlas scheduler", "09:30 cycle skipped — a cycle was "
                       "already running", priority="high")
        if now >= pending["backup"]:
            pending["backup"] = next_fire(now, BACKUP_UTC)
            await asyncio.to_thread(_run_backup)
