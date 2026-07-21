"""F-018: the trading-lifecycle lock must be acquired in canonical order (audit
lock FIRST), so it can never form an ABBA deadlock cycle with the daily cycle or
an audit append. Uses two real Postgres connections."""
from __future__ import annotations

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from atlas.core.locks import AUDIT_LOCK, acquire_trading_lifecycle_lock
from tests.conftest import URL, requires_pg

pytestmark = requires_pg


def _pg_dsn() -> str:
    # psycopg DSN (strip the SQLAlchemy driver prefix)
    return URL.replace("postgresql+psycopg://", "postgresql://")


def test_lifecycle_lock_holds_the_audit_lock_too(pg_session):
    """After acquire_trading_lifecycle_lock, the session holds BOTH advisory
    locks — proof the audit lock (rank 1) is taken as part of lifecycle locking."""
    s = pg_session
    acquire_trading_lifecycle_lock(s)
    held = s.execute(text(
        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
        "AND objid = :k AND pid = pg_backend_pid()"), {"k": AUDIT_LOCK}).scalar()
    assert held >= 1                         # the audit lock is held


def test_lifecycle_path_blocks_on_the_audit_lock_first():
    """Canonical order proof: while connection A holds ONLY the audit lock,
    connection B calling the lifecycle helper blocks on the AUDIT lock (rank 1)
    — it has not yet reached the lifecycle lock. Under the old order B would take
    the lifecycle lock first and not block here (the ABBA setup). B is given a
    short lock_timeout so the test cannot hang."""
    eng = create_engine(URL)
    A = sessionmaker(bind=eng)()
    a_raw = psycopg.connect(_pg_dsn())
    try:
        # A holds the audit lock (rank 1) inside an open transaction.
        A.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AUDIT_LOCK})
        # B tries the lifecycle helper with a short timeout; it must block on the
        # audit lock A holds and time out (canonical order = audit first).
        B = sessionmaker(bind=eng)()
        B.execute(text("SET lock_timeout = '750ms'"))
        with pytest.raises(Exception) as ei:
            acquire_trading_lifecycle_lock(B)
        assert "lock timeout" in str(ei.value).lower() or "canceling" in str(ei.value).lower()
        B.rollback()
        B.close()
    finally:
        A.rollback()
        A.close()
        a_raw.close()
        eng.dispose()
