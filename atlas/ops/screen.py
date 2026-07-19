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

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.core.db import session_scope
from atlas.dcp.research.opportunity_screen import (
    SCREEN_SOURCE,
    screen_opportunities,
    snapshot_board_picks,
)

_screen_lock = threading.Lock()
_status: dict[str, object] = {"phase": "idle", "started_at": None,
                              "finished_at": None, "detail": None, "board": None}

_snapshot_lock = threading.Lock()
_snap_status: dict[str, object] = {"phase": "idle", "started_at": None,
                                   "finished_at": None, "detail": None, "result": None}


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


# ---- snapshot the board's top-K into MEASURED source-pick tracking ----------

def _run_snapshot(top_k: int) -> None:
    rec_date = datetime.now(UTC).date()
    with session_scope() as s:            # commits the recorded picks
        rows = snapshot_board_picks(s, rec_date, top_k=top_k)
    rec = sum(1 for _sym, o in rows if o == "recorded")
    dup = sum(1 for _sym, o in rows if o == "duplicate")
    nod = sum(1 for _sym, o in rows if o == "no-data")
    _snap_status.update(
        phase="done", finished_at=datetime.now(UTC).isoformat(),
        detail=(f"top {len(rows)} of the board recorded as '{'atlas-opportunity-screen'}' "
                f"picks for {rec_date} — recorded {rec}, duplicate {dup}, no-data {nod}; "
                f"tracked vs SPY, edge shows once matured (~20 sessions)"),
        result={"recorded": rec, "duplicate": dup, "no_data": nod,
                "date": rec_date.isoformat(),
                "rows": [{"symbol": sym, "outcome": o} for sym, o in rows]})


def start_snapshot(top_k: int = 20) -> bool:
    """Console trigger: record the current board's top-K into research.source_picks
    for edge measurement. One at a time; 'busy' is an honest answer. MEASURED,
    NEVER APPLIED — the picks are scored vs SPY, never bridged to capital."""
    if not _snapshot_lock.acquire(blocking=False):
        return False
    _snap_status.update(phase="running", started_at=datetime.now(UTC).isoformat(),
                        finished_at=None, detail="ranking + snapshotting features",
                        result=None)

    def _target() -> None:
        try:
            _run_snapshot(top_k)
        except Exception as e:  # noqa: BLE001 — the ops layer survives anything
            _snap_status.update(phase="failed",
                                finished_at=datetime.now(UTC).isoformat(),
                                detail=str(e)[:300])
        finally:
            _snapshot_lock.release()

    thread = threading.Thread(target=_target, name="atlas-screen-snapshot", daemon=True)
    try:
        thread.start()
    except Exception:
        _snapshot_lock.release()
        raise
    return True


def snapshot_status() -> dict[str, object]:
    """Snapshot copy of the last board-snapshot job (same discipline as above)."""
    out = dict(_snap_status)
    if isinstance(out.get("result"), dict):
        out["result"] = dict(out["result"])  # type: ignore[arg-type]
    out["running"] = _snapshot_lock.locked()
    return out


# ---- monthly cohort, recorded by the daily cycle (T9) -----------------------

MONTHLY_TOP_K = 20


def monthly_snapshot_if_due(session: Session, clock: Clock, *,
                            top_k: int = MONTHLY_TOP_K) -> str:
    """ONE board cohort per calendar month, recorded automatically by the daily
    cycle so the screen's edge measurement sustains itself with no console
    click. SELF-HEALING, not calendar-pinned: the first cycle of a month that
    finds no cohort yet records one (a machine asleep on the 1st records on the
    2nd — the cohort is dated the day it was actually knowable, never
    backdated). Idempotent on top of record_pick's (source, ticker, date)
    conflict rule, so a checkpoint replay of the same day is a no-op.

    A MANUAL console TRACK earlier in the month counts as that month's cohort —
    deliberate: one recording event per month keeps cohorts non-overlapping, and
    topping a partial cohort up on a later date would either backdate picks or
    double-count tickers across dates. A partial cohort (< top_k) is therefore
    LATCHED for the month, but never silently: the nightly line labels it
    partial, every cycle, so the Principal sees the thin month all month. An
    empty board records nothing and reports that honestly — the guard stays
    open and the next cycle retries.

    Runs on the cycle's INJECTABLE clock (invariant 6 — this is pipeline code,
    unlike the console-triggered jobs above whose wall clock is ops-layer
    WHEN-deciding), normalized to UTC like every session-date decision in the
    cycle. Deterministic, no model spend, MEASURED-NEVER-APPLIED: cohort rows
    are scored vs SPY + dartboard by grade_picks, never bridged."""
    today = clock.now().astimezone(UTC).date()
    # bounded to <= today: a stray future-dated row (manual API misuse) must
    # never suppress months of cohorts ahead of itself
    row = session.execute(text(
        "SELECT count(*) AS n, min(recommendation_date) AS d "
        "FROM research.source_picks "
        "WHERE source = :s AND recommendation_date >= :m "
        "  AND recommendation_date <= :t"),
        {"s": SCREEN_SOURCE, "m": today.replace(day=1), "t": today}).one()
    if row.n:
        # the cohort's date is printed so a hand-timed (console TRACK) cohort is
        # visible as such in every nightly line — the date choice is the one
        # bias the substitution rule admits, so it is never hidden
        partial = (" — PARTIAL, < top-" + str(top_k)) if row.n < top_k else ""
        return (f"screen cohort exists for {today:%Y-%m} "
                f"({row.n} picks, dated {row.d}{partial})")
    rows = snapshot_board_picks(session, today, top_k=top_k)
    rec = sum(1 for _sym, o in rows if o == "recorded")
    nod = sum(1 for _sym, o in rows if o == "no-data")
    if rec == 0:
        return (f"screen cohort EMPTY for {today:%Y-%m} — nothing recordable "
                f"({len(rows)} board rows, {nod} no-data); retrying next cycle")
    line = f"screen cohort recorded for {today:%Y-%m}: {rec} picks"
    return line + (f", {nod} no-data" if nod else "")
