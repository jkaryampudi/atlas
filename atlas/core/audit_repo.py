"""Postgres-backed audit log (Doc 05 §6). Append-only; chain math from core.audit.

Appends are serialised with a transaction-scoped advisory lock so concurrent writers
cannot fork the chain. Each append also updates the protected terminal anchor
(audit.chain_head, F-020) atomically. verify() re-walks the entire chain from the
database and checks it against the anchor.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit import (CHAIN_EPOCH, GENESIS_HASH, AuditEvent, ChainAnchor,
                              link_hash, payload_hash, verify_chain)
from atlas.core.clock import Clock
from atlas.core.locks import AUDIT_LOCK

_LOCK_KEY = AUDIT_LOCK  # advisory lock namespace for the audit chain (F-018: canonical rank 1)


def _chain_hash_of(row: Any) -> str:
    """Chain hash of a stored row under ITS OWN epoch (legacy rows verify under
    epoch 1; epoch-2 rows fold in the identity columns)."""
    return link_hash(row["prev_hash"], row["payload_hash"], row["event_type"],
                     row["created_at"], hash_version=row.get("hash_version", 1),
                     entity_type=row["entity_type"] or "", entity_id=row["entity_id"] or "",
                     actor_type=row["actor_type"] or "", actor_id=row["actor_id"] or "")


class PostgresAuditLog:
    def __init__(self, session: Session, clock: Clock) -> None:
        self._s = session
        self._clock = clock

    def append(self, *, event_type: str, entity_type: str, entity_id: str,
               actor_type: str, actor_id: str, payload: dict[str, Any]) -> AuditEvent:
        s = self._s
        s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
        # F-020: authorise this transaction to write the protected anchor.
        s.execute(text("SET LOCAL atlas.audit_writer = 'on'"))
        last = s.execute(text(
            "SELECT seq, prev_hash, payload_hash, event_type, created_at, hash_version, "
            "entity_type, entity_id, actor_type, actor_id "
            "FROM audit.decision_events ORDER BY seq DESC LIMIT 1")).mappings().first()
        prev = _chain_hash_of(last) if last else GENESIS_HASH
        created_at = self._clock.now()
        p_hash = payload_hash(payload)
        row = s.execute(text(
            "INSERT INTO audit.decision_events "
            "(event_type, entity_type, entity_id, actor_type, actor_id, payload, "
            " payload_hash, prev_hash, created_at, hash_version) "
            "VALUES (:et, :ent, :eid, :at, :aid, CAST(:p AS jsonb), :ph, :prev, :ca, :hv) "
            "RETURNING seq"),
            {"et": event_type, "ent": entity_type, "eid": entity_id, "at": actor_type,
             "aid": actor_id, "p": json.dumps(payload), "ph": p_hash,
             "prev": prev, "ca": created_at, "hv": CHAIN_EPOCH}).mappings().one()
        ev = AuditEvent(seq=row["seq"], event_type=event_type, entity_type=entity_type,
                        entity_id=entity_id, actor_type=actor_type, actor_id=actor_id,
                        payload=payload, payload_hash=p_hash, prev_hash=prev,
                        created_at=created_at, hash_version=CHAIN_EPOCH)
        # F-020: advance the protected terminal anchor atomically with the append.
        # event_count is a MONOTONIC +1, never count(*): a blind recompute would
        # SELF-HEAL the anchor after a tail delete (delete N rows, append one, and
        # count(*) matches the truncated chain again), masking truncation. The
        # increment plus the migration-0042 monotonic guard mean a truncated walk
        # can never re-match the anchor. The initial insert (fresh chain_head) uses
        # the real count; deletes are DB-refused, so it cannot be gamed.
        s.execute(text(
            "INSERT INTO audit.chain_head (id, last_seq, last_chain_hash, event_count, "
            " epoch, updated_at) VALUES "
            "(1, :seq, :ch, (SELECT count(*) FROM audit.decision_events), :ep, :ts) "
            "ON CONFLICT (id) DO UPDATE SET last_seq = :seq, last_chain_hash = :ch, "
            " event_count = audit.chain_head.event_count + 1, "
            " epoch = :ep, updated_at = :ts"),
            {"seq": ev.seq, "ch": ev.chain_hash, "ep": CHAIN_EPOCH, "ts": created_at})
        return ev

    def verify(self) -> int:
        s = self._s
        rows = s.execute(text(
            "SELECT seq, event_type, entity_type, entity_id, actor_type, actor_id, "
            "payload, payload_hash, prev_hash, created_at, hash_version "
            "FROM audit.decision_events ORDER BY seq")).mappings()
        events = (AuditEvent(seq=r["seq"], event_type=r["event_type"],
                             entity_type=r["entity_type"] or "", entity_id=r["entity_id"] or "",
                             actor_type=r["actor_type"], actor_id=r["actor_id"] or "",
                             payload=r["payload"], payload_hash=r["payload_hash"],
                             prev_hash=r["prev_hash"], created_at=r["created_at"],
                             hash_version=r["hash_version"])
                  for r in rows)
        a = s.execute(text(
            "SELECT last_seq, last_chain_hash, event_count, epoch "
            "FROM audit.chain_head WHERE id = 1")).mappings().first()
        anchor = (ChainAnchor(last_seq=a["last_seq"], last_chain_hash=a["last_chain_hash"],
                              event_count=a["event_count"], epoch=a["epoch"])
                  if a is not None else None)
        return verify_chain(events, anchor=anchor)
