"""OPPORTUNITY SCREEN (ops trigger): run the whole-universe deterministic screen
on demand from the console and hold the ranked board for the API to serve.

Mirrors atlas/ops/analyze.py exactly — a non-blocking lock (one screen at a
time; "busy" is an answer, not an error), a background worker thread, and a
module-level status dict the API polls. The screen itself is pure DCP research
(no LLM, no model spend); wall clock here is legitimate ops WHEN-deciding — the
as_of is the ops layer's "today", and the screen applies point-in-time bounds
below it (latest fundamentals <= as_of, closes <= as_of).

MEASURED, NEVER APPLIED. The board is a research candidate list; it reaches no
sizing / pricing / execution, and any systematic rule built on it must clear the
full gauntlet + a signature first (see opportunity_screen.py)."""
from __future__ import annotations

import threading
from datetime import UTC, datetime

from atlas.core.db import session_scope
from atlas.dcp.research.opportunity_screen import screen_opportunities

_screen_lock = threading.Lock()
_status: dict[str, object] = {"phase": "idle", "started_at": None,
                              "finished_at": None, "detail": None, "board": None}


def _run_screen(top_n: int) -> None:
    started = datetime.now(UTC)
    as_of = started.date()
    with session_scope() as s:
        out = screen_opportunities(s, as_of, top_n=top_n)
    _status.update(phase="done", finished_at=datetime.now(UTC).isoformat(),
                   detail=(f"ranked {out['ranked_n']} of {out['universe_n']} names "
                           f"as of {out['as_of']}"),
                   board=out)


def start_screen(top_n: int = 25) -> bool:
    """Console trigger. Returns False when a screen is already running — one at a
    time; the caller reports 'busy' honestly, nothing runs twice."""
    if not _screen_lock.acquire(blocking=False):
        return False
    _status.update(phase="running", started_at=datetime.now(UTC).isoformat(),
                   finished_at=None, detail="ranking the universe", board=None)

    def _target() -> None:
        try:
            _run_screen(top_n)
        except Exception as e:  # noqa: BLE001 — the ops layer survives anything
            _status.update(phase="failed",
                           finished_at=datetime.now(UTC).isoformat(),
                           detail=str(e)[:300])
        finally:
            _screen_lock.release()

    thread = threading.Thread(target=_target, name="atlas-screen", daemon=True)
    try:
        thread.start()
    except Exception:
        # the release lives in _target's finally, which never runs if the thread
        # failed to start — release here so a screen can never wedge permanently
        _screen_lock.release()
        raise
    return True


def screen_status() -> dict[str, object]:
    """Snapshot copy (same discipline as analysis_status): mutating the returned
    dict must never reach the live status. The last board persists across polls
    until the next run replaces it."""
    out = dict(_status)
    out["running"] = _screen_lock.locked()
    return out
