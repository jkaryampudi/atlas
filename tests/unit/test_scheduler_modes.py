"""ATLAS_INPROC_SCHEDULER modes (post-relocation, 2026-08-30): '1' fires and
supervises (the pre-launchd interim), 'supervise' runs ONLY the F-025
supervisor while launchd owns the fire times, anything else runs nothing.
Two schedulers double-fire; zero supervisors lose the stuck-run safety net."""
from __future__ import annotations

import asyncio

import atlas.ops.scheduler as sched
from atlas.ops.scheduler import loop_for_mode, scheduler_loop, supervisor_loop


def test_mode_selection():
    assert loop_for_mode("1") is scheduler_loop
    assert loop_for_mode("supervise") is supervisor_loop
    assert loop_for_mode(None) is None
    assert loop_for_mode("0") is None
    assert loop_for_mode("") is None


def test_supervisor_loop_supervises_at_boot_then_on_cadence(monkeypatch):
    calls: list[bool] = []

    async def fake_supervise(startup: bool) -> None:
        calls.append(startup)

    real_sleep = asyncio.sleep          # captured BEFORE patching (sched.asyncio is the global module)

    async def fast_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(sched, "_supervise_safely", fake_supervise)
    monkeypatch.setattr(sched.asyncio, "sleep", fast_sleep)

    async def drive() -> None:
        task = asyncio.create_task(supervisor_loop())
        for _ in range(50):
            await asyncio.sleep(0)
            if len(calls) >= 3:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert calls[0] is True                 # boot recovery first
    assert calls[1:3] == [False, False]     # then periodic supervision only


def test_status_reports_launchd_fires_under_supervise(monkeypatch):
    """Under 'supervise' the console must see launchd's LOCAL 07:00 / 08:00
    fires (in UTC), not the retired in-process 23:30 / 00:30 UTC times it
    showed until 2026-08-30. Under '1' the in-process times stand; with no
    mode nothing fires from here and the fields say so."""
    from datetime import UTC, datetime, timedelta

    from atlas.ops import scheduler as sched

    now = datetime(2026, 8, 30, 1, 25, tzinfo=UTC)         # 11:25 AEST Sunday
    monkeypatch.setattr(sched, "_WALL", type("W", (), {"now": lambda self: now})())

    monkeypatch.setenv("ATLAS_INPROC_SCHEDULER", "supervise")
    st = sched.status()
    assert st["mode"] == "supervise" and st["fires_owned_by"] == "launchd"
    nxt = datetime.fromisoformat(str(st["next_cycle_utc"]))
    local = nxt.astimezone()
    assert (local.hour, local.minute) == (7, 0)             # launchd's local hour
    assert now < nxt <= now + timedelta(days=1)
    bkp = datetime.fromisoformat(str(st["next_backup_utc"]))
    assert bkp.astimezone().hour == 8 and bkp > nxt

    monkeypatch.setenv("ATLAS_INPROC_SCHEDULER", "1")
    st = sched.status()
    assert st["mode"] == "inproc"
    assert str(st["next_cycle_utc"]).endswith("T23:30:00+00:00")

    monkeypatch.delenv("ATLAS_INPROC_SCHEDULER")
    st = sched.status()
    assert st["mode"] == "off" and st["next_cycle_utc"] is None


def test_next_fire_local_rolls_forward_and_requires_aware():
    from datetime import UTC, datetime, time

    import pytest

    from atlas.ops.scheduler import next_fire_local

    at = time(7, 0)
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    nxt = next_fire_local(now, at)
    assert nxt > now and nxt.astimezone().hour == 7
    assert next_fire_local(nxt, at) > nxt                    # strictly after
    with pytest.raises(ValueError):
        next_fire_local(datetime(2026, 8, 30, 12, 0), at)
