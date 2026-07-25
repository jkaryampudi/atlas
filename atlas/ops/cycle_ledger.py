"""Durable daily-cycle ledger (F-025 / M56 / M57).

Writes ops.cycle_runs on a connection SEPARATE from the daily cycle's atomic
session_scope(), so the 'running' claim and the terminal row (with the captured
LLM spend) survive the very rollback they exist to record. Each function opens
its own session_scope() — a fresh session is a fresh pooled connection with its
own transaction, committed independently of the cycle's.

Never write cycle-ledger rows on the cycle's own session: that is the bug this
fixes (workflow_runs, reconciliation breaks and audit events all vanish on the
kill they are meant to record).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.core.db import session_scope

# terminal statuses (running is the only non-terminal one)
TERMINAL = ("completed", "failed", "refused", "killed")
# The live cycle refreshes heartbeat_at this often (daily.main's daemon), so a
# heartbeat older than several intervals means the process is dead or hung.
HEARTBEAT_SECONDS = 60.0


class CycleOverlapError(RuntimeError):
    """A cycle is already RUNNING for this business date (the durable
    cross-process overlap guard fired). The caller must not start a second."""


def open_cycle(business_date: date, run_id: str, trigger: str, *, clock: Clock,
               host: str | None = None, pid: int | None = None) -> str:
    """Claim the business date with a durable 'running' row (autonomous commit).
    Raises CycleOverlapError if another run already holds the date (the partial
    unique index on status='running'). Returns the new cycle_runs id."""
    now = clock.now()
    try:
        with session_scope() as s:
            cid = s.execute(text(
                "INSERT INTO ops.cycle_runs (business_date, run_id, trigger, "
                " status, started_at, heartbeat_at, host, pid) "
                "VALUES (:d, :r, :t, 'running', :now, :now, :h, :p) "
                "RETURNING id"),
                {"d": business_date, "r": run_id, "t": trigger, "now": now,
                 "h": host, "p": pid}).scalar()
        return str(cid)
    except IntegrityError as e:
        # the only unique index that can fire on a 'running' insert is
        # uq_cycle_runs_running -> another cycle already holds this date.
        raise CycleOverlapError(
            f"a cycle is already running for {business_date}") from e


def heartbeat(cycle_id: str, *, clock: Clock) -> None:
    """Refresh the liveness marker of a running cycle (autonomous)."""
    with session_scope() as s:
        s.execute(text(
            "UPDATE ops.cycle_runs SET heartbeat_at = :now "
            "WHERE id = :id AND status = 'running'"),
            {"now": clock.now(), "id": cycle_id})


def close_cycle(cycle_id: str, status: str, *, clock: Clock,
                exit_code: int | None = None, llm_spend: Decimal | None = None,
                detail: str | None = None) -> None:
    """Write the terminal row (autonomous commit). status in TERMINAL. Records
    exit_code, the LLM spend captured before the cycle's rollback (M57), and a
    truncated detail. Idempotent-safe: only updates a row still 'running'."""
    if status not in TERMINAL:
        raise ValueError(f"close_cycle status must be terminal, got {status!r}")
    with session_scope() as s:
        s.execute(text(
            "UPDATE ops.cycle_runs SET status = :st, finished_at = :now, "
            " exit_code = :ec, llm_spend_usd = :sp, "
            " detail = left(:d, 2000) "
            "WHERE id = :id AND status = 'running'"),
            {"st": status, "now": clock.now(), "ec": exit_code,
             "sp": llm_spend, "d": detail, "id": cycle_id})


def day_llm_spend(session: Session, business_date: date) -> Decimal:
    """Sum of research.agent_runs.cost_usd for the date, read on the GIVEN session
    (so it sees the cycle's uncommitted spend rows before they roll back). Call
    this inside the cycle session, before session_scope exits, to capture the
    spend for the durable ledger row (M57). Best-effort: returns 0 if the query
    fails (e.g. an aborted transaction after a DB error)."""
    try:
        val = session.execute(text(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM research.agent_runs "
            "WHERE created_at::date = :d"), {"d": business_date}).scalar()
        return Decimal(str(val or 0))
    except Exception:
        return Decimal(0)


def completed_dates(session: Session, since: date) -> set[date]:
    """Business dates with at least one 'completed' cycle_runs row on/after
    `since`. The supervisor's 'this day ran' set."""
    rows = session.execute(text(
        "SELECT DISTINCT business_date FROM ops.cycle_runs "
        "WHERE status = 'completed' AND business_date >= :since"),
        {"since": since}).all()
    return {r.business_date for r in rows}


def attempted_dates(session: Session, since: date) -> set[date]:
    """Business dates that were at least ATTEMPTED on/after `since` — a row whose
    status is completed/failed/killed/running. A date with ONLY a 'refused' row
    (a manual too-early click that did not consume the day) or NO row is NOT
    attempted, so it is a candidate for the missed-cycle dead-man."""
    rows = session.execute(text(
        "SELECT DISTINCT business_date FROM ops.cycle_runs "
        "WHERE business_date >= :since "
        "  AND status IN ('completed','failed','killed','running')"),
        {"since": since}).all()
    return {r.business_date for r in rows}


def earliest_business_date(session: Session) -> date | None:
    """The oldest business_date on record — the anchor that stops the supervisor
    alerting for dates before the ledger existed (cold-start flood guard)."""
    return session.execute(text(
        "SELECT min(business_date) FROM ops.cycle_runs")).scalar()


@dataclass(frozen=True)
class RunningCycle:
    id: str
    business_date: date
    run_id: str
    started_at: datetime
    host: str | None
    pid: int | None
    last_seen: datetime          # heartbeat, or start until the first beat


def running_cycles(session: Session) -> list[RunningCycle]:
    """Every 'running' row with the fields the supervisor needs to judge
    liveness — the recorded (host, pid) is the authoritative crashed-vs-alive
    signal on this host; last_seen (heartbeat, or start until the first beat) is
    the hung-but-alive signal. The supervisor, not this query, decides."""
    rows = session.execute(text(
        "SELECT id, business_date, run_id, started_at, host, pid, "
        "       COALESCE(heartbeat_at, started_at) AS last_seen "
        "FROM ops.cycle_runs WHERE status = 'running' ORDER BY business_date")).all()
    return [RunningCycle(id=str(r.id), business_date=r.business_date,
                         run_id=r.run_id, started_at=r.started_at, host=r.host,
                         pid=r.pid, last_seen=r.last_seen) for r in rows]


def mark_killed(cycle_id: str, *, clock: Clock, detail: str) -> None:
    """Recovery: rewrite a stale 'running' row to 'killed' so the date is no
    longer blocked for a retry and the interruption is durably visible."""
    with session_scope() as s:
        s.execute(text(
            "UPDATE ops.cycle_runs SET status = 'killed', finished_at = :now, "
            " detail = left(:d, 2000) WHERE id = :id AND status = 'running'"),
            {"now": clock.now(), "d": detail, "id": cycle_id})
