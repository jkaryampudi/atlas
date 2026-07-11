from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from atlas.core.audit import ChainVerificationError
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import SystemClock
from atlas.core.db import session_scope

router = APIRouter()


@router.get("/events/verify")
def verify() -> dict[str, object]:
    """A broken chain is a STRUCTURED state, never a 500 — tampering must be
    distinguishable from an API outage (Doc 08 standing kill condition)."""
    with session_scope() as s:
        try:
            n = PostgresAuditLog(s, SystemClock()).verify()
            return {"chain": "ok", "events_verified": n,
                    "break_at_seq": None, "reason": None}
        except ChainVerificationError as e:
            return {"chain": "broken", "events_verified": None,
                    "break_at_seq": e.seq, "reason": e.reason}


@router.get("/events")
def events(limit: int = 50) -> list[dict[str, object]]:
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT seq, event_type, entity_type, entity_id, actor_type, created_at "
            "FROM audit.decision_events ORDER BY seq DESC LIMIT :n"), {"n": limit}).mappings()
        return [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]
