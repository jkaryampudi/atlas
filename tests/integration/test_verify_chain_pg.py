"""Nightly chain-verification job (task 2): a clean chain verifies and logs its
own pass; tampering or deletion anywhere breaks verification loudly.

The UPDATE/DELETE statements here simulate an attacker/bug in the TEST database
only — detecting exactly this is the job's purpose (invariant 4)."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.tools.verify_chain import run
from tests.conftest import requires_pg

pytestmark = requires_pg
CLOCK = FrozenClock(datetime(2026, 7, 11, 3, 0, tzinfo=UTC))


def _append(s, n: int) -> None:
    log = PostgresAuditLog(s, CLOCK)
    for i in range(n):
        log.append(event_type="test.event", entity_type="test", entity_id=str(i),
                   actor_type="scheduler", actor_id="test", payload={"i": i})


def test_clean_chain_verifies_and_logs_its_own_pass(clean_audit):
    s = clean_audit
    _append(s, 3)
    assert run(s, CLOCK) == 3
    # the verification event itself joined the chain and verifies next time
    assert run(s, CLOCK) == 4
    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type='audit.chain.verified' ORDER BY seq LIMIT 1")).scalar()
    assert ev["verified_events"] == 3


def test_payload_tamper_is_refused(clean_audit):
    """Migration 0042: the payload is immutable (append-only) — the tamper is now
    PREVENTED at the DB, not merely detected afterwards. (verify_chain's payload-
    hash detection is retained and unit-tested in test_audit_chain.py.)"""
    s = clean_audit
    _append(s, 2)
    with pytest.raises(ProgrammingError, match="append-only"):
        s.execute(text("UPDATE audit.decision_events "
                       "SET payload = '{\"i\": 999}' WHERE seq = 1"))


def test_row_deletion_is_refused(clean_audit):
    s = clean_audit
    _append(s, 3)
    with pytest.raises(ProgrammingError, match="append-only"):
        s.execute(text("DELETE FROM audit.decision_events WHERE seq = 2"))
