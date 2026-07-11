"""Append-only audit event hash chain (Doc 05 par.6).

Every material platform event becomes an AuditEvent whose payload_hash covers a canonical
JSON serialisation and whose prev_hash links to the previous event. Tampering with any
historical payload breaks every subsequent link. verify_chain re-walks the chain and is
run nightly (and in CI against fixtures).

This module is pure logic — persistence lives in the audit repository so the chain math
is unit-testable without Postgres.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

GENESIS_HASH = "0" * 64

_ALLOWED_ACTORS = {"dcp", "agent", "human", "scheduler", "broker"}


def canonical_json(payload: dict[str, Any]) -> str:
    """Deterministic serialisation: sorted keys, no whitespace variance."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def link_hash(prev_hash: str, p_hash: str, event_type: str, created_at: datetime) -> str:
    material = f"{prev_hash}|{p_hash}|{event_type}|{created_at.isoformat()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    event_type: str
    entity_type: str
    entity_id: str
    actor_type: str
    actor_id: str
    payload: dict[str, Any]
    payload_hash: str
    prev_hash: str  # link hash of the previous event (GENESIS_HASH for seq 1)
    created_at: datetime

    @property
    def chain_hash(self) -> str:
        return link_hash(self.prev_hash, self.payload_hash, self.event_type, self.created_at)


class ChainBuilder:
    """Builds a valid chain in memory; the DB repository mirrors this on INSERT."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def append(self, *, event_type: str, entity_type: str, entity_id: str, actor_type: str,
               actor_id: str, payload: dict[str, Any], created_at: datetime) -> AuditEvent:
        if actor_type not in _ALLOWED_ACTORS:
            raise ValueError(f"unknown actor_type {actor_type!r}")
        prev = self._events[-1].chain_hash if self._events else GENESIS_HASH
        ev = AuditEvent(
            seq=len(self._events) + 1,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=dict(payload),
            payload_hash=payload_hash(payload),
            prev_hash=prev,
            created_at=created_at,
        )
        self._events.append(ev)
        return ev


class ChainVerificationError(Exception):
    def __init__(self, seq: int, reason: str) -> None:
        self.seq = seq
        self.reason = reason
        super().__init__(f"audit chain broken at seq={seq}: {reason}")


def verify_chain(events: Iterable[AuditEvent]) -> int:
    """Re-walk the chain. Returns number of verified events; raises on any break.

    Sequence numbers must be strictly increasing but need not be contiguous: Postgres
    sequences advance on rolled-back transactions, so benign gaps occur. Deletions are
    still detected — removing any row breaks the next row's prev_hash link.
    """
    prev = GENESIS_HASH
    count = 0
    last_seq = 0
    for ev in events:
        if ev.seq <= last_seq:
            raise ChainVerificationError(ev.seq, f"non-monotonic seq (last {last_seq})")
        if payload_hash(ev.payload) != ev.payload_hash:
            raise ChainVerificationError(ev.seq, "payload hash mismatch (tamper?)")
        if ev.prev_hash != prev:
            raise ChainVerificationError(ev.seq, "prev_hash link mismatch")
        prev = ev.chain_hash
        count += 1
        last_seq = ev.seq
    return count
