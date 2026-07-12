"""Dual-confirmation clearance of a latched DD2/DD3 breaker (Doc 04 §5).

"Resumption from DD2/DD3 requires the dual-confirmation human action" and
"Breaker state changes are audit events and cannot be cleared by agents."
This module is that action — deterministic compute plane, human-actor-only by
construction (the API routes here; agents hold no write path to risk.*).
request_clearance records confirmation A; confirm_clearance records
confirmation B, which is refused before requested_at + 1h with the Doc 06
§3.3 error code DUAL_CONFIRM_TOO_SOON (the ≥1h gap is ALSO a table CHECK, so
even a hand-written UPDATE cannot confirm early). Only a CONFIRMED row feeds
human_cleared=True into the breaker fold — and the fold steps down to the
COMPUTED target for the live drawdown, so clearing during a still-DD2-deep
drawdown leaves DD2 in force: you clear a latched memory of a drawdown,
never a live one.

Documented resolutions:
- Import direction: this module imports the fold helpers from
  atlas.dcp.trading.proposals. That is cycle-free (proposals imports
  atlas.dcp.risk.engine, which knows nothing of clearances) and keeps the
  fold in ONE place next to its NAV-history source instead of splitting it
  into a shared module for import aesthetics.
- The "current latched breaker" both functions consult is the fold over
  PERSISTED snapshots + confirmed clearances only — never a live mark of the
  book. Marking fails closed on stale closes/missing FX, and an unmarkable
  unrelated holding must not lock the principal out of the resumption action
  (same reasoning as the exit paths in atlas.dcp.trading.exits).
- One pending request at a time: a second confirmation A while one awaits
  confirmation B would let the requester bank multiple future clearances.
- reason is required and non-blank: the audit trail of a breaker clearance
  without a reason is not an audit trail (Doc 04 §5 post-mortem/review
  lineage).
- Both entrypoints take the lifecycle advisory lock so clearances serialise
  with snapshots/approvals — the fold everyone reads moves atomically.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.risk.engine import BreakerLevel
from atlas.dcp.trading.proposals import (
    _audit,
    _breaker_fold,
    _confirmed_clearances,
    _lifecycle_lock,
    _snapshot_navs,
)

DUAL_CONFIRM_GAP = timedelta(hours=1)   # Doc 06 §2: second confirmation ≥1h later


def latched_breaker_level(session: Session) -> BreakerLevel:
    """Latched breaker from persisted NAV history + confirmed clearances —
    the book-independent view (module docstring: never marks the book)."""
    return _breaker_fold(_snapshot_navs(session), _confirmed_clearances(session))


def request_clearance(session: Session, clock: Clock, *, reason: str,
                      actor: str = "principal") -> str:
    """Confirmation A. Valid only while the latched breaker is DD2/DD3 and no
    other request is pending. Returns the clearance id; audited."""
    _lifecycle_lock(session)
    if not reason.strip():
        raise ValueError("a breaker clearance requires a non-blank reason — "
                         "the audit trail must say why (Doc 04 §5)")
    level = latched_breaker_level(session)
    if level not in (BreakerLevel.DD2, BreakerLevel.DD3):
        raise ValueError(f"nothing to clear: latched breaker is {level.value} — "
                         "only DD2/DD3 require the dual-confirmation resumption "
                         "(Doc 04 §5)")
    pending = session.execute(text(
        "SELECT id FROM risk.breaker_clearances WHERE confirmed_at IS NULL "
        "LIMIT 1")).first()
    if pending is not None:
        raise ValueError(f"a clearance request is already pending ({pending.id}) — "
                         "confirm it after the 1h gap; multiple pending "
                         "confirmations A cannot be banked")
    now = clock.now()
    clearance_id = session.execute(text(
        "INSERT INTO risk.breaker_clearances "
        "(from_level, reason, requested_by, requested_at, created_at) "
        "VALUES (:lvl, :r, :by, :at, :at) RETURNING id"),
        {"lvl": level.value, "r": reason, "by": actor, "at": now}).scalar_one()
    _audit(session, clock).append(
        event_type="drawdown.breaker.clear_requested",
        entity_type="breaker_clearance", entity_id=str(clearance_id),
        actor_type="human", actor_id=actor,
        payload={"clearance_id": str(clearance_id), "from_level": level.value,
                 "reason": reason, "requested_at": now.isoformat(),
                 "confirmable_after": (now + DUAL_CONFIRM_GAP).isoformat()})
    return str(clearance_id)


def confirm_clearance(session: Session, clock: Clock, *, clearance_id: str,
                      actor: str = "principal") -> BreakerLevel:
    """Confirmation B, ≥1h after A (DUAL_CONFIRM_TOO_SOON otherwise, Doc 06
    §3.3). Sets confirmed_at — from that instant the fold evaluates with
    human_cleared=True — and returns the recomputed latched level; audited."""
    _lifecycle_lock(session)
    row = session.execute(text(
        "SELECT id, from_level, reason, requested_at, confirmed_at "
        "FROM risk.breaker_clearances WHERE id = :c FOR UPDATE"),
        {"c": UUID(clearance_id)}).first()
    if row is None:
        raise ValueError(f"unknown clearance {clearance_id}")
    if row.confirmed_at is not None:
        raise ValueError(f"clearance {clearance_id} is already confirmed — "
                         "a clearance clears once")
    now = clock.now()
    not_before = row.requested_at + DUAL_CONFIRM_GAP
    if now < not_before:
        raise ValueError(
            f"DUAL_CONFIRM_TOO_SOON: confirmation B at {now.isoformat()} is "
            f"before {not_before.isoformat()} — dual confirmation requires the "
            "two actions ≥1h apart (Doc 06 §2, Doc 04 §5)")
    session.execute(text(
        "UPDATE risk.breaker_clearances SET confirmed_at = :at WHERE id = :c"),
        {"at": now, "c": row.id})
    new_level = latched_breaker_level(session)   # now includes this clearance
    _audit(session, clock).append(
        event_type="drawdown.breaker.cleared",
        entity_type="breaker_clearance", entity_id=str(row.id),
        actor_type="human", actor_id=actor,
        payload={"clearance_id": str(row.id), "from_level": row.from_level,
                 "to": new_level.value, "reason": row.reason,
                 "confirmed_at": now.isoformat()})
    return new_level
