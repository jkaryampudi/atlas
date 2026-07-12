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
                            "ok": None, "detail": None}


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
    The pipeline does its own alerting; this reports only the envelope."""
    _last.update(started_at=datetime.now(UTC).isoformat(), finished_at=None,
                 ok=None, detail="running")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "atlas.ops.daily"], cwd=_REPO,
            capture_output=True, text=True, timeout=3600)
        ok = proc.returncode == 0
        detail = (proc.stdout or proc.stderr or "").strip()[-500:]
        if not ok:
            notify("Atlas daily cycle FAILED",
                   f"exit {proc.returncode} — console jobs board has the step",
                   priority="high")
    except Exception as e:  # noqa: BLE001 — the scheduler must survive anything
        ok, detail = False, f"cycle runner crashed: {e}"
        notify("Atlas daily cycle CRASHED", str(e), priority="high")
    _last.update(finished_at=datetime.now(UTC).isoformat(), ok=ok, detail=detail)


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
    return {"cycle_running": _cycle_lock.locked(),
            "next_cycle_utc": next_fire(now, CYCLE_UTC).isoformat(),
            "next_backup_utc": next_fire(now, BACKUP_UTC).isoformat(),
            "last": dict(_last)}


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


async def scheduler_loop() -> None:
    """Sleep until the nearest of the two daily fire times, run it, repeat.
    Started from the API's lifespan when ATLAS_INPROC_SCHEDULER=1."""
    while True:
        now = datetime.now(UTC)
        fires = [(next_fire(now, CYCLE_UTC), "cycle"),
                 (next_fire(now, BACKUP_UTC), "backup")]
        when, what = min(fires)
        await asyncio.sleep(max(1.0, (when - datetime.now(UTC)).total_seconds()))
        if what == "cycle":
            if not start_cycle():
                notify("Atlas scheduler", "09:30 cycle skipped — a cycle was "
                       "already running", priority="high")
        else:
            await asyncio.to_thread(_run_backup)
