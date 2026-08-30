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
