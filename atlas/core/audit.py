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

# F-019: the hash EPOCH. Epoch 1 (legacy) covered only prev|payload|event_type|
# created_at, so an event's actor/entity identity was outside the chain and could
# be rewritten undetected. Epoch 2 folds the material identity columns into the
# link hash. Each stored event carries its own hash_version so the legacy chain
# still verifies under epoch 1 while every NEW event uses epoch 2. Do not lower
# CHAIN_EPOCH: raising it is a reviewed migration (see migration 0036).
CHAIN_EPOCH = 2


def canonical_json(payload: dict[str, Any]) -> str:
    """Deterministic serialisation: sorted keys, no whitespace variance."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def link_hash(prev_hash: str, p_hash: str, event_type: str, created_at: datetime,
              *, hash_version: int = 1, entity_type: str = "", entity_id: str = "",
              actor_type: str = "", actor_id: str = "") -> str:
    """Chain link hash. Epoch 1 (legacy) is unchanged so the historical chain
    still verifies byte-identically. Epoch 2 (F-019) additionally binds the
    material identity columns (entity_type/id, actor_type/id) — and a version tag
    so the two epochs can never collide — so rewriting who/what an event
    references breaks the chain."""
    if hash_version >= 2:
        material = "|".join(("v2", prev_hash, p_hash, event_type,
                             created_at.isoformat(), entity_type, entity_id,
                             actor_type, actor_id))
    else:
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
    hash_version: int = 1  # F-019: which hash epoch this event was written under

    @property
    def chain_hash(self) -> str:
        return link_hash(self.prev_hash, self.payload_hash, self.event_type,
                         self.created_at, hash_version=self.hash_version,
                         entity_type=self.entity_type or "", entity_id=self.entity_id or "",
                         actor_type=self.actor_type or "", actor_id=self.actor_id or "")


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
            hash_version=CHAIN_EPOCH,
        )
        self._events.append(ev)
        return ev


class ChainVerificationError(Exception):
    def __init__(self, seq: int, reason: str) -> None:
        self.seq = seq
        self.reason = reason
        super().__init__(f"audit chain broken at seq={seq}: {reason}")


@dataclass(frozen=True)
class ChainAnchor:
    """F-020: the independently-recorded terminal state of the chain. Deleting or
    replacing tail events changes the walked (count, last_seq, last_chain_hash)
    without breaking any surviving prev_hash link, so an interior-only walk cannot
    detect truncation. The anchor is compared against the walk to catch it."""
    last_seq: int
    last_chain_hash: str
    event_count: int
    epoch: int


def verify_chain(events: Iterable[AuditEvent], *, anchor: ChainAnchor | None = None) -> int:
    """Re-walk the chain. Returns number of verified events; raises on any break.

    Sequence numbers must be strictly increasing but need not be contiguous: Postgres
    sequences advance on rolled-back transactions, so benign gaps occur. Interior
    deletion/tamper is detected — removing or rewriting a row breaks the next row's
    prev_hash link, and (epoch 2) rewriting actor/entity identity breaks it too.

    F-020: when ``anchor`` is supplied, the walked terminal state (event_count,
    last_seq, last_chain_hash) must equal it — this catches TAIL truncation /
    terminal-event replacement / head rollback, which the interior walk cannot.
    """
    prev = GENESIS_HASH
    count = 0
    last_seq = 0
    last_chain_hash = GENESIS_HASH
    for ev in events:
        if ev.seq <= last_seq:
            raise ChainVerificationError(ev.seq, f"non-monotonic seq (last {last_seq})")
        if payload_hash(ev.payload) != ev.payload_hash:
            raise ChainVerificationError(ev.seq, "payload hash mismatch (tamper?)")
        if ev.prev_hash != prev:
            raise ChainVerificationError(ev.seq, "prev_hash link mismatch")
        last_chain_hash = ev.chain_hash
        prev = last_chain_hash
        count += 1
        last_seq = ev.seq
    if anchor is not None:
        if count != anchor.event_count:
            raise ChainVerificationError(
                last_seq, f"event-count mismatch: walked {count} but anchor records "
                f"{anchor.event_count} (tail truncation / deletion?)")
        if last_seq != anchor.last_seq:
            raise ChainVerificationError(
                last_seq, f"terminal seq mismatch: walked {last_seq} but anchor "
                f"records {anchor.last_seq}")
        if last_chain_hash != anchor.last_chain_hash:
            raise ChainVerificationError(
                last_seq, "terminal chain-hash mismatch (terminal event replaced / "
                "tail rewritten?)")
    return count
