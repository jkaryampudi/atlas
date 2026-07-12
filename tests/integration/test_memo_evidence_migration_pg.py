"""Migration 0013 cycle: downgrade removes research.memo_evidence, upgrade
restores it with the provenance shape — UNIQUE(memo_id, ordinal), NOT NULL
ref/body, and the FK to research.memos are structural, not application
convention. The bodies a memo was argued from are a historical fact; the
table must make ambiguity (two rows at one ordinal) impossible."""
from __future__ import annotations

import os
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg


def _alembic(*args: str) -> None:
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(["alembic", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr}"


def _table_exists() -> bool:
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            return c.execute(text(
                "SELECT to_regclass('research.memo_evidence')")).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0013_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _table_exists()          # at head, the table is there
        _alembic("downgrade", "0012")
        assert not _table_exists()      # clean removal
        _alembic("upgrade", "head")
        assert _table_exists()          # clean re-creation
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _seeded_memo(s) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        "conviction, thesis, evidence_refs) "
        "VALUES ('committee', 'ZMEV', 'WATCHLIST', 'LOW', 'migration test memo', "
        "'[]') RETURNING id")).scalar())


def test_ordinal_is_unique_per_memo(pg_session):
    s = pg_session
    memo_id = _seeded_memo(s)
    ins = text("INSERT INTO research.memo_evidence (memo_id, ordinal, ref, body) "
               "VALUES (:m, 0, :ref, 'the exact text the agents read')")
    s.execute(ins, {"m": memo_id, "ref": "dcp:bars:ZMEV:2026-07-10"})
    with pytest.raises(IntegrityError):
        s.execute(ins, {"m": memo_id, "ref": "dcp:indicators:ZMEV:2026-07-10"})


def test_body_and_ref_are_not_nullable(pg_session):
    s = pg_session
    memo_id = _seeded_memo(s)
    with pytest.raises(IntegrityError):
        s.execute(text(
            "INSERT INTO research.memo_evidence (memo_id, ordinal, ref, body) "
            "VALUES (:m, 0, 'dcp:bars:ZMEV:2026-07-10', NULL)"), {"m": memo_id})


def test_memo_fk_is_enforced(pg_session):
    with pytest.raises(IntegrityError):
        pg_session.execute(text(
            "INSERT INTO research.memo_evidence (memo_id, ordinal, ref, body) "
            "VALUES (gen_random_uuid(), 0, 'ref', 'orphan body')"))
