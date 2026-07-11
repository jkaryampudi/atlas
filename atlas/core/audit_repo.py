"""Postgres-backed audit log (Doc 05 §6). Append-only; chain math from core.audit.

Appends are serialised with a transaction-scoped advisory lock so concurrent writers
cannot fork the chain. verify() re-walks the entire chain from the database.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit import (GENESIS_HASH, AuditEvent, link_hash, payload_hash,
                              verify_chain)
from atlas.core.clock import Clock

_LOCK_KEY = 762001  # advisory lock namespace for the audit chain


class PostgresAuditLog:
    def __init__(self, session: Session, clock: Clock) -> None:
        self._s = session
        self._clock = clock

    def append(self, *, event_type: str, entity_type: str, entity_id: str,
               actor_type: str, actor_id: str, payload: dict[str, Any]) -> AuditEvent:
        s = self._s
        s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
        last = s.execute(text(
            "SELECT seq, prev_hash, payload_hash, event_type, created_at "
            "FROM audit.decision_events ORDER BY seq DESC LIMIT 1")).mappings().first()
        prev = (link_hash(last["prev_hash"], last["payload_hash"],
                          last["event_type"], last["created_at"])
                if last else GENESIS_HASH)
        created_at = self._clock.now()
        p_hash = payload_hash(payload)
        row = s.execute(text(
            "INSERT INTO audit.decision_events "
            "(event_type, entity_type, entity_id, actor_type, actor_id, payload, "
            " payload_hash, prev_hash, created_at) "
            "VALUES (:et, :ent, :eid, :at, :aid, CAST(:p AS jsonb), :ph, :prev, :ca) "
            "RETURNING seq"),
            {"et": event_type, "ent": entity_type, "eid": entity_id, "at": actor_type,
             "aid": actor_id, "p": json.dumps(payload), "ph": p_hash,
             "prev": prev, "ca": created_at}).mappings().one()
        return AuditEvent(seq=row["seq"], event_type=event_type, entity_type=entity_type,
                          entity_id=entity_id, actor_type=actor_type, actor_id=actor_id,
                          payload=payload, payload_hash=p_hash, prev_hash=prev,
                          created_at=created_at)

    def verify(self) -> int:
        rows = self._s.execute(text(
            "SELECT seq, event_type, entity_type, entity_id, actor_type, actor_id, "
            "payload, payload_hash, prev_hash, created_at "
            "FROM audit.decision_events ORDER BY seq")).mappings()
        events = (AuditEvent(seq=r["seq"], event_type=r["event_type"],
                             entity_type=r["entity_type"] or "", entity_id=r["entity_id"] or "",
                             actor_type=r["actor_type"], actor_id=r["actor_id"] or "",
                             payload=r["payload"], payload_hash=r["payload_hash"],
                             prev_hash=r["prev_hash"], created_at=r["created_at"])
                  for r in rows)
        return verify_chain(events)
