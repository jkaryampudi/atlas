"""Integration-test database isolation.

ALL Postgres-backed tests run against a dedicated `atlas_test` database, never the
shared dev DB: fixtures here TRUNCATE tables, and the dev DB holds real backfilled
history and the live audit chain. The test DB is created and migrated on demand.
A hard guard refuses to hand out sessions when the connected database is not the
test database — destructive fixtures must be impossible to point at real data.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).parents[1]
ADMIN_URL = os.environ.get(
    "ATLAS_DATABASE_URL",
    "postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas")
TEST_DB_NAME = "atlas_test"
URL = os.environ.get(
    "ATLAS_TEST_DATABASE_URL",
    ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB_NAME}")

_prepared = False


def _reachable() -> bool:
    try:
        create_engine(ADMIN_URL).connect().close()
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(not _reachable(), reason="postgres not reachable")


def _ensure_test_db() -> None:
    """Create atlas_test if missing and migrate it to head. Runs once per session."""
    global _prepared
    if _prepared:
        return
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                           {"n": TEST_DB_NAME}).scalar()
        if not exists:
            c.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(["alembic", "upgrade", "head"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"alembic upgrade on {TEST_DB_NAME} failed:\n{r.stderr}")
    _prepared = True


def _assert_test_db(session) -> None:
    """Guard: destructive fixtures must never touch anything but atlas_test."""
    name = session.execute(text("SELECT current_database()")).scalar()
    if name != TEST_DB_NAME:
        pytest.fail(f"REFUSING to run destructive test fixtures against database "
                    f"{name!r} — tests may only touch {TEST_DB_NAME!r}")


@pytest.fixture
def pg_session():
    _ensure_test_db()
    engine = create_engine(URL)
    s = sessionmaker(bind=engine)()
    _assert_test_db(s)
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def clean_audit(pg_session):
    _assert_test_db(pg_session)
    pg_session.execute(text(
        "TRUNCATE audit.decision_events, research.memos, research.agent_runs "
        "RESTART IDENTITY CASCADE"))
    pg_session.commit()
    yield pg_session
