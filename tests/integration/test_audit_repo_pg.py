from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg


def test_append_and_verify_roundtrip(clean_audit):
    s = clean_audit
    log = PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 11, 6, 0, tzinfo=UTC)))
    for i in range(10):
        log.append(event_type="test.event", entity_type="t", entity_id=str(i),
                   actor_type="dcp", actor_id="itest", payload={"i": i})
    s.commit()
    assert log.verify() == 10


def test_db_tamper_is_refused(clean_audit):
    """Migration 0042: an in-place edit of a persisted audit row is REFUSED by the
    append-only guard — the tamper cannot happen (stronger than the prior
    detect-after-the-fact). The untouched chain still verifies."""
    s = clean_audit
    log = PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 11, 6, 0, tzinfo=UTC)))
    for i in range(5):
        log.append(event_type="test.event", entity_type="t", entity_id=str(i),
                   actor_type="dcp", actor_id="itest", payload={"i": i})
    s.commit()
    with pytest.raises(ProgrammingError, match="append-only"):
        s.execute(text("UPDATE audit.decision_events SET payload='{\"i\": 999}' WHERE seq=3"))
    s.rollback()
    assert log.verify() == 5
