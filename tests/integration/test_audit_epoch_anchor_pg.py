"""F-019 (hash epoch covers actor/entity identity) and F-020 (protected tail
anchor detects truncation). The UPDATE/DELETE statements simulate an
attacker/bug in the disposable test database."""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import text

from atlas.core.audit import ChainVerificationError
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import URL, requires_pg

pytestmark = requires_pg
CLOCK = FrozenClock(datetime(2026, 7, 21, 12, tzinfo=UTC))


def _append(s, n: int) -> None:
    log = PostgresAuditLog(s, CLOCK)
    for i in range(n):
        log.append(event_type="test.event", entity_type="proposal", entity_id=str(i),
                   actor_type="human", actor_id=f"alice-{i}", payload={"i": i})


# --------------------------------------------------------------------------- #
# F-019 — actor / entity identity is inside the hash (epoch 2)
# --------------------------------------------------------------------------- #
def test_interior_actor_id_tamper_is_detected(clean_audit):
    s = clean_audit
    _append(s, 3)
    s.execute(text("UPDATE audit.decision_events SET actor_id = 'mallory' WHERE seq = 2"))
    with pytest.raises(ChainVerificationError):
        PostgresAuditLog(s, CLOCK).verify()


def test_interior_entity_id_tamper_is_detected(clean_audit):
    s = clean_audit
    _append(s, 3)
    s.execute(text("UPDATE audit.decision_events SET entity_id = '999' WHERE seq = 2"))
    with pytest.raises(ChainVerificationError):
        PostgresAuditLog(s, CLOCK).verify()


def test_tail_actor_tamper_is_caught_by_the_anchor(clean_audit):
    """Rewriting the LAST event's actor breaks no interior prev_hash link (there
    is no next event) — only the epoch-2 identity hash + the anchor catch it."""
    s = clean_audit
    _append(s, 3)
    s.execute(text("UPDATE audit.decision_events SET actor_id = 'mallory' "
                   "WHERE seq = (SELECT max(seq) FROM audit.decision_events)"))
    with pytest.raises(ChainVerificationError):
        PostgresAuditLog(s, CLOCK).verify()


# --------------------------------------------------------------------------- #
# F-020 — tail truncation / terminal replacement detected via the anchor
# --------------------------------------------------------------------------- #
def test_tail_deletion_is_detected(clean_audit):
    """The exact blind spot the review probed: deleting the FINAL event leaves the
    surviving prefix internally valid, but the anchor mismatches."""
    s = clean_audit
    _append(s, 3)
    s.execute(text("DELETE FROM audit.decision_events "
                   "WHERE seq = (SELECT max(seq) FROM audit.decision_events)"))
    with pytest.raises(ChainVerificationError) as ei:
        PostgresAuditLog(s, CLOCK).verify()
    assert "tail" in str(ei.value).lower() or "mismatch" in str(ei.value).lower()


def test_multi_tail_deletion_is_detected(clean_audit):
    s = clean_audit
    _append(s, 5)
    s.execute(text("DELETE FROM audit.decision_events WHERE seq >= "
                   "(SELECT max(seq) - 1 FROM audit.decision_events)"))
    with pytest.raises(ChainVerificationError):
        PostgresAuditLog(s, CLOCK).verify()


def test_full_truncate_of_events_is_detected(clean_audit):
    """Truncating the events but not the anchor is caught (count 0 vs anchor)."""
    s = clean_audit
    _append(s, 3)
    s.execute(text("DELETE FROM audit.decision_events"))
    with pytest.raises(ChainVerificationError):
        PostgresAuditLog(s, CLOCK).verify()


def test_interior_deletion_still_detected(clean_audit):
    s = clean_audit
    _append(s, 3)
    s.execute(text("DELETE FROM audit.decision_events WHERE seq = 2"))
    with pytest.raises(ChainVerificationError):
        PostgresAuditLog(s, CLOCK).verify()


def test_clean_chain_with_anchor_verifies(clean_audit):
    s = clean_audit
    _append(s, 4)
    assert PostgresAuditLog(s, CLOCK).verify() == 4     # anchor matches the walk


# --------------------------------------------------------------------------- #
# F-020 — the anchor row is protected from ordinary writers
# --------------------------------------------------------------------------- #
def test_anchor_is_protected_from_outside_writers():
    """A connection that has NOT set the audit-writer GUC cannot UPDATE or DELETE
    the anchor row (the migration's BEFORE UPDATE/DELETE trigger). Uses a fresh
    committed connection against the seeded anchor (empty-DB seed: count 0)."""
    dsn = URL.replace("postgresql+psycopg://", "postgresql://")
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        # ensure a row exists to target (idempotent seed)
        conn.execute("INSERT INTO audit.chain_head (id,last_seq,last_chain_hash,"
                     "event_count,epoch) VALUES (1,0,'"+"0"*64+"',0,1) "
                     "ON CONFLICT (id) DO NOTHING")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE audit.chain_head SET last_seq = 999 WHERE id = 1")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM audit.chain_head WHERE id = 1")
    finally:
        conn.close()
