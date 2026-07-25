"""F-019/F-020: the least-privilege runtime role (atlas_app, migration 0043)
cannot bypass the audit append-only + monotonic-anchor triggers.

The audit wall is only real if the runtime connects as a NON-superuser, NON-owner
role: a superuser turns every row trigger off with
``SET session_replication_role='replica'``. These tests connect AS ``atlas_app``
and prove it is refused every bypass vector, while the owner ``atlas`` (the
current dev/test connection) is a superuser and is flagged as such by the guard.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.core.db_privilege import (
    assert_audit_wall_enforced,
    assert_least_privilege_runtime,
    audit_triggers_always_enabled,
    runtime_privilege,
)
from datetime import UTC, datetime
from tests.conftest import URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

APP_URL = make_url(URL).set(username="atlas_app", password="atlas_app_local_only")


@pytest.fixture(scope="module")
def app_engine():
    """An engine connected as the least-privilege atlas_app role (created by
    migration 0043 when the test DB is built)."""
    _ensure_test_db()
    eng = create_engine(APP_URL)
    yield eng
    eng.dispose()


def _blocked(engine, sql: str) -> bool:
    """True when `sql` is refused (each attempt gets its own connection, since a
    permission error aborts the transaction)."""
    with engine.connect() as c:
        try:
            c.execute(text(sql))
            c.commit()
            return False
        except Exception:
            return True


# ---------------------------------------------------------------- posture

def test_runtime_role_is_least_privilege(app_engine):
    with sessionmaker(bind=app_engine)() as s:
        p = runtime_privilege(s)
    assert p.role == "atlas_app"
    assert p.is_superuser is False
    assert p.can_set_replication_role is False
    assert p.least_privilege is True


def test_owner_connection_is_superuser_and_flagged(pg_session):
    """The current dev/test connection (owner `atlas`) IS a superuser — the exact
    posture the guard must reject in a least-privilege deployment."""
    p = runtime_privilege(pg_session)
    assert p.is_superuser is True
    assert p.least_privilege is False


def test_guard_passes_for_app_role_and_raises_for_superuser(app_engine, pg_session):
    with sessionmaker(bind=app_engine)() as s:
        assert_least_privilege_runtime(s)          # least-privilege: no raise
    with pytest.raises(RuntimeError, match="bypass the audit"):
        assert_least_privilege_runtime(pg_session)  # superuser: fail closed


# ------------------------------------------------------- bypass vectors

def test_cannot_set_session_replication_role(app_engine):
    assert _blocked(app_engine, "SET session_replication_role = 'replica'")


def test_cannot_disable_audit_triggers(app_engine):
    assert _blocked(app_engine, "ALTER TABLE audit.decision_events DISABLE TRIGGER ALL")


def test_cannot_drop_the_append_only_trigger(app_engine):
    assert _blocked(
        app_engine,
        "DROP TRIGGER decision_events_append_only ON audit.decision_events")


def test_cannot_update_audit_events(app_engine):
    assert _blocked(app_engine,
                    "UPDATE audit.decision_events SET event_type = 'x'")


def test_cannot_delete_audit_events(app_engine):
    assert _blocked(app_engine, "DELETE FROM audit.decision_events")


def test_cannot_delete_the_chain_head_anchor(app_engine):
    assert _blocked(app_engine, "DELETE FROM audit.chain_head")


def test_cannot_alter_protected_objects(app_engine):
    """A non-owner cannot alter the audit table structure (e.g. drop a column)."""
    assert _blocked(app_engine,
                    "ALTER TABLE audit.decision_events DROP COLUMN IF EXISTS payload")


# ---------------------------------------------------------- still functional

def test_runtime_role_can_read_the_app(app_engine):
    """Least privilege is not no privilege: the role reads the app tables it
    needs (proving the grants are wired, not just the denials)."""
    with app_engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM market.instruments")).scalar()
    assert n is not None


def test_runtime_role_has_append_but_not_mutate_on_audit(app_engine):
    with app_engine.connect() as c:
        priv = c.execute(text(
            "SELECT has_table_privilege('atlas_app','audit.decision_events','INSERT') AS ins, "
            "has_table_privilege('atlas_app','audit.decision_events','UPDATE') AS upd, "
            "has_table_privilege('atlas_app','audit.decision_events','DELETE') AS del")
        ).mappings().one()
    assert priv["ins"] is True
    assert priv["upd"] is False and priv["del"] is False


# ---------------------------------------- ENABLE ALWAYS (migration 0044) ----

def test_audit_triggers_are_enable_always(pg_session):
    """F-019/F-020: migration 0044 sets both audit guards to ENABLE ALWAYS
    (tgenabled='A'), so replica mode cannot suppress them."""
    assert audit_triggers_always_enabled(pg_session) is True
    assert_audit_wall_enforced(pg_session)          # does not raise


def test_replica_mode_cannot_suppress_audit_triggers_even_for_owner():
    """The reproduced bypass, now CLOSED: even the OWNER superuser, having set
    session_replication_role='replica', cannot UPDATE/DELETE an audit row or lower
    the anchor — the ENABLE ALWAYS trigger (0044) still fires. Runs on a dedicated
    owner connection (each attack on its own transaction; a trigger error aborts
    it) and cleans up in `finally`."""
    _ensure_test_db()
    eng = create_engine(URL)                  # owner (superuser) connection
    Sess = sessionmaker(bind=eng)
    try:
        with Sess() as s:
            s.execute(text("TRUNCATE audit.decision_events, audit.chain_head "
                           "RESTART IDENTITY CASCADE"))
            s.commit()
            PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 25, 12, tzinfo=UTC))).append(
                event_type="research.scorecard.updated", entity_type="scorecard",
                entity_id="2026-07-25", actor_type="dcp", actor_id="scorecard",
                payload={"k": "v"})
            s.commit()
            assert s.execute(text("SELECT count(*) FROM audit.decision_events")).scalar() >= 1
        for sql in ("UPDATE audit.decision_events SET event_type='TAMPERED'",
                    "DELETE FROM audit.decision_events",
                    "UPDATE audit.chain_head SET last_seq = 0, event_count = 0"):
            with Sess() as s:
                s.execute(text("SET session_replication_role = 'replica'"))
                assert s.execute(text("SHOW session_replication_role")).scalar() == "replica"
                raised = False
                try:
                    s.execute(text(sql))
                    s.commit()
                except Exception:
                    raised = True
                    s.rollback()
                assert raised, f"replica mode did NOT block: {sql}"
    finally:
        with Sess() as s:
            s.execute(text("TRUNCATE audit.decision_events, audit.chain_head "
                           "RESTART IDENTITY CASCADE"))
            s.commit()
        eng.dispose()
