"""In-process scheduler (the console-only control path): fire-time math and
the never-fire-twice lock."""
from datetime import UTC, datetime, time

import pytest

from atlas.ops import scheduler
from atlas.ops.scheduler import CYCLE_UTC, next_fire


def test_next_fire_same_day_before_the_hour():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    assert next_fire(now, CYCLE_UTC) == datetime(2026, 7, 13, 23, 30, tzinfo=UTC)


def test_next_fire_rolls_to_tomorrow_after_the_hour():
    now = datetime(2026, 7, 13, 23, 45, tzinfo=UTC)
    assert next_fire(now, CYCLE_UTC) == datetime(2026, 7, 14, 23, 30, tzinfo=UTC)


def test_next_fire_exact_instant_rolls_forward():
    now = datetime(2026, 7, 13, 23, 30, tzinfo=UTC)  # strictly after: tomorrow
    assert next_fire(now, CYCLE_UTC) == datetime(2026, 7, 14, 23, 30, tzinfo=UTC)


def test_next_fire_requires_aware_datetime():
    with pytest.raises(ValueError, match="aware"):
        next_fire(datetime(2026, 7, 13, 10, 0), time(23, 30))


def test_start_cycle_never_fires_twice(monkeypatch):
    """The lock answers 'busy' honestly; it never queues a second run."""
    import threading

    release = threading.Event()
    ran = threading.Event()

    def fake_cycle():
        ran.set()
        release.wait(timeout=5)

    monkeypatch.setattr(scheduler, "_run_cycle", fake_cycle)
    assert scheduler.start_cycle() is True
    assert ran.wait(timeout=5)
    assert scheduler.start_cycle() is False       # busy — nothing fired twice
    assert scheduler.status()["cycle_running"] is True
    release.set()
    for _ in range(100):                          # lock frees after the run
        if not scheduler.status()["cycle_running"]:
            break
        import time as _t
        _t.sleep(0.05)
    assert scheduler.status()["cycle_running"] is False
    assert scheduler.start_cycle() is True        # and a new run may start
    release.set()


def test_run_daily_endpoint_reports_started_and_busy(monkeypatch):
    from fastapi.testclient import TestClient

    from atlas.api.main import app

    calls = {"n": 0}

    def fake_start():
        calls["n"] += 1
        return calls["n"] == 1                    # first starts, second is busy

    monkeypatch.setattr(scheduler, "start_cycle", fake_start)
    with TestClient(app) as c:
        assert c.post("/v1/system/run-daily").json()["started"] is True
        assert c.post("/v1/system/run-daily").json()["started"] is False
        st = c.get("/v1/system/scheduler").json()
        assert "next_cycle_utc" in st and "cycle_running" in st


def test_emit_line_format(capsys):
    from atlas.ops.daily import _emit

    _emit("t3_settle", "done", "fills=1")
    out = capsys.readouterr().out.strip()
    assert out.startswith("@@CYCLE ")
    import json

    ev = json.loads(out[8:])
    assert (ev["node"], ev["status"], ev["result"]) == ("t3_settle", "done", "fills=1")
    assert "at" in ev


def test_status_progress_is_a_snapshot_copy():
    scheduler._last["progress"] = [{"node": "t0_ingest", "status": "running"}]
    snap = scheduler.status()["last"]["progress"]
    snap[0]["status"] = "mutated"
    assert scheduler._last["progress"][0]["status"] == "running"
