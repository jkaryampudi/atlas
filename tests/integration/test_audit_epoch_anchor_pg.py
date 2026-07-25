"""F-019 (identity is inside the hash) and F-020 (protected, monotonic tail anchor).

Migration 0042 upgraded the guarantee from DETECT to PREVENT: audit.decision_events
is append-only at the DB (no UPDATE/DELETE of any row, legacy or new), and the
chain_head anchor is monotonic + undeletable regardless of the writer GUC. So the
attacker's UPDATE/DELETE now RAISES at the database — the tamper cannot happen —
rather than succeeding and being caught later. The detection layer (verify_chain +
anchor) is retained as defense-in-depth and unit-tested in test_audit.py against
in-memory chains.
"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import URL, requires_pg

pytestmark = requires_pg
CLOCK = FrozenClock(datetime(2026, 7, 21, 12, tzinfo=UTC))
_APPEND_ONLY = "append-only"


def _append(s, n: int) -> None:
    log = PostgresAuditLog(s, CLOCK)
    for i in range(n):
        log.append(event_type="test.event", entity_type="proposal", entity_id=str(i),
                   actor_type="human", actor_id=f"alice-{i}", payload={"i": i})


# --------------------------------------------------------------------------- #
# F-019 — identity columns are IMMUTABLE (append-only guard); a legacy or new
# row's actor/entity can no longer be rewritten at all.
# --------------------------------------------------------------------------- #
def test_interior_actor_id_tamper_is_refused(clean_audit):
    s = clean_audit
    _append(s, 3)
    with pytest.raises(ProgrammingError, match=_APPEND_ONLY):
        s.execute(text("UPDATE audit.decision_events SET actor_id='mallory' WHERE seq=2"))


def test_interior_entity_id_tamper_is_refused(clean_audit):
    s = clean_audit
    _append(s, 3)
    with pytest.raises(ProgrammingError, match=_APPEND_ONLY):
        s.execute(text("UPDATE audit.decision_events SET entity_id='999' WHERE seq=2"))


def test_legacy_hash_version_row_identity_tamper_is_refused(clean_audit):
    """The exact F-019 hole: a legacy hash_version=1 row's identity columns were
    outside the epoch-1 hash. They are now IMMUTABLE — even inserting a legacy-epoch
    row and trying to rewrite its actor is refused by the append-only guard."""
    s = clean_audit
    _append(s, 2)
    # force a row to look like a legacy epoch-1 event (DDL/INSERT is allowed) ...
    s.execute(text(
        "INSERT INTO audit.decision_events (event_type, entity_type, entity_id, "
        " actor_type, actor_id, payload, payload_hash, prev_hash, created_at, "
        " hash_version) SELECT event_type, entity_type, entity_id, actor_type, "
        " actor_id, payload, payload_hash, prev_hash, created_at, 1 "
        "FROM audit.decision_events ORDER BY seq DESC LIMIT 1"))
    # ... but its identity can never be rewritten
    with pytest.raises(ProgrammingError, match=_APPEND_ONLY):
        s.execute(text("UPDATE audit.decision_events SET actor_id='mallory' "
                       "WHERE hash_version = 1"))


def test_tail_actor_tamper_is_refused(clean_audit):
    s = clean_audit
    _append(s, 3)
    with pytest.raises(ProgrammingError, match=_APPEND_ONLY):
        s.execute(text("UPDATE audit.decision_events SET actor_id='mallory' "
                       "WHERE seq=(SELECT max(seq) FROM audit.decision_events)"))


# --------------------------------------------------------------------------- #
# F-020 — tail / interior deletion is IMPOSSIBLE (append-only guard), and the
# anchor can neither be lowered nor deleted (monotonic guard).
# --------------------------------------------------------------------------- #
def test_tail_deletion_is_refused(clean_audit):
    s = clean_audit
    _append(s, 3)
    with pytest.raises(ProgrammingError, match=_APPEND_ONLY):
        s.execute(text("DELETE FROM audit.decision_events "
                       "WHERE seq=(SELECT max(seq) FROM audit.decision_events)"))


def test_interior_deletion_is_refused(clean_audit):
    s = clean_audit
    _append(s, 3)
    with pytest.raises(ProgrammingError, match=_APPEND_ONLY):
        s.execute(text("DELETE FROM audit.decision_events WHERE seq=2"))


def test_bulk_deletion_is_refused(clean_audit):
    s = clean_audit
    _append(s, 3)
    with pytest.raises(ProgrammingError, match=_APPEND_ONLY):
        s.execute(text("DELETE FROM audit.decision_events"))


def test_clean_chain_with_anchor_verifies(clean_audit):
    s = clean_audit
    _append(s, 4)
    assert PostgresAuditLog(s, CLOCK).verify() == 4     # anchor matches the walk


def test_anchor_is_protected_and_monotonic():
    """F-020: even a connection that SETS the writer GUC (any DB writer can) cannot
    LOWER the anchor to match a truncated chain, nor delete it. Without the GUC it
    cannot write it at all. Uses a fresh committed connection."""
    dsn = URL.replace("postgresql+psycopg://", "postgresql://")
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute("SET atlas.audit_writer = 'on'")
        conn.execute("INSERT INTO audit.chain_head (id,last_seq,last_chain_hash,"
                     "event_count,epoch) VALUES (1,10,'" + "0" * 64 + "',10,2) "
                     "ON CONFLICT (id) DO UPDATE SET last_seq=10, event_count=10")
        # monotonic: lowering last_seq or event_count is refused EVEN with the GUC
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE audit.chain_head SET last_seq=5 WHERE id=1")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE audit.chain_head SET event_count=3 WHERE id=1")
        # deletion is refused unconditionally
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM audit.chain_head WHERE id=1")
        # advancing (monotonic increase) is allowed for the writer
        conn.execute("UPDATE audit.chain_head SET last_seq=11, event_count=11 WHERE id=1")
        # a NON-writer (GUC unset) cannot write it at all
        conn.execute("RESET atlas.audit_writer")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE audit.chain_head SET last_seq=12 WHERE id=1")
    finally:
        conn.close()
