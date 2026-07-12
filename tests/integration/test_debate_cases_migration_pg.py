"""Migration 0019 cycle: downgrade removes research.memo_debate, upgrade
restores it with the provenance shape — UNIQUE(memo_id, role), the four-seat
role CHECK matching DebateResult's actual structure, NOT NULL payload, and the
FK to research.memos are structural, not application convention. What the CIO
read is a historical fact; the table must make ambiguity (two bulls on one
memo, a seat that does not exist) impossible."""
from __future__ import annotations

import importlib.util
import os
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

_MIGRATION = ROOT / "migrations" / "versions" / "0019_debate_cases.py"


def _down_revision() -> str:
    """Read 0019's down_revision from the file itself, so this test follows
    the chain if the migration is re-based under concurrent work."""
    spec = importlib.util.spec_from_file_location("mig_0019", _MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.down_revision)


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
                "SELECT to_regclass('research.memo_debate')")).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0019_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _table_exists()          # at head, the table is there
        _alembic("downgrade", _down_revision())
        assert not _table_exists()      # clean removal
        _alembic("upgrade", "head")
        assert _table_exists()          # clean re-creation
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _seeded_memo(s) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        "conviction, thesis, evidence_refs) "
        "VALUES ('committee', 'ZMDB', 'WATCHLIST', 'LOW', 'migration test memo', "
        "'[]') RETURNING id")).scalar())


_INSERT = text("INSERT INTO research.memo_debate (memo_id, role, payload) "
               "VALUES (:m, :role, CAST(:p AS jsonb))")


def test_one_case_per_seat_per_memo(pg_session):
    s = pg_session
    memo_id = _seeded_memo(s)
    s.execute(_INSERT, {"m": memo_id, "role": "bull", "p": '{"stance": "BULL"}'})
    with pytest.raises(IntegrityError):
        s.execute(_INSERT, {"m": memo_id, "role": "bull",
                            "p": '{"stance": "BULL", "later": true}'})


def test_role_check_matches_the_actual_debate_structure(pg_session):
    s = pg_session
    memo_id = _seeded_memo(s)
    for role in ("bull", "bear", "bull_rebuttal", "bear_rebuttal"):
        s.execute(_INSERT, {"m": memo_id, "role": role, "p": "{}"})
    with pytest.raises(IntegrityError):
        s.execute(_INSERT, {"m": memo_id, "role": "moderator", "p": "{}"})


def test_payload_is_not_nullable(pg_session):
    s = pg_session
    memo_id = _seeded_memo(s)
    with pytest.raises(IntegrityError):
        s.execute(text("INSERT INTO research.memo_debate (memo_id, role, payload) "
                       "VALUES (:m, 'bear', NULL)"), {"m": memo_id})


def test_memo_fk_is_enforced(pg_session):
    with pytest.raises(IntegrityError):
        pg_session.execute(_INSERT, {"m": "00000000-0000-0000-0000-000000000000",
                                     "role": "bull", "p": "{}"})
