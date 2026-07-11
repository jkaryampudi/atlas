from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from atlas.core.audit import ChainVerificationError
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


def test_db_tamper_breaks_chain(clean_audit):
    s = clean_audit
    log = PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 11, 6, 0, tzinfo=UTC)))
    for i in range(5):
        log.append(event_type="test.event", entity_type="t", entity_id=str(i),
                   actor_type="dcp", actor_id="itest", payload={"i": i})
    s.commit()
    # simulate a malicious in-place edit (superuser can; app roles cannot)
    s.execute(text("UPDATE audit.decision_events SET payload = '{\"i\": 999}' WHERE seq = 3"))
    s.commit()
    with pytest.raises(ChainVerificationError):
        log.verify()
