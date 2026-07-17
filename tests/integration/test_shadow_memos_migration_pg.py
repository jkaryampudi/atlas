"""Migration 0029 cycle: downgrade removes research.shadow_memos, upgrade
restores it with the non-actionable separation shape — the FK to
research.memos (a shadow row must point at a real production memo), NOT NULL
payload/challenger_model/comparison_id, and UNIQUE(comparison_id,
source_memo_id) (one challenger re-run per source memo per comparison) are
structural, not application convention. Shadow outputs living in their OWN
table — never research.memos — is the Constitution 7.2 guarantee this table
exists to make unforgeable."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

_MIGRATION = ROOT / "migrations" / "versions" / "0029_shadow_memos.py"


def _down_revision() -> str:
    """Read 0029's down_revision from the file itself, so this test follows
    the chain if the migration is re-based under concurrent work."""
    spec = importlib.util.spec_from_file_location("mig_0029", _MIGRATION)
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
                "SELECT to_regclass('research.shadow_memos')")).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0029_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _table_exists()          # at head, the table is there
        _alembic("downgrade", _down_revision())
        assert not _table_exists()      # clean removal
        _alembic("upgrade", "head")
        assert _table_exists()          # clean re-creation
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _seed_memo(s) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        " recommendation, conviction) "
        "VALUES ('committee', 'SHDW', 'REJECT', 'LOW') RETURNING id")).scalar_one())


def _insert_shadow(s, memo_id: str, comparison_id: str) -> None:
    s.execute(text(
        "INSERT INTO research.shadow_memos "
        "(source_memo_id, challenger_model, comparison_id, payload) "
        "VALUES (CAST(:m AS uuid), 'claude-sonnet-5', :cid, CAST(:p AS jsonb))"),
        {"m": memo_id, "cid": comparison_id, "p": json.dumps({"memo": {}})})


def test_shadow_memos_structural_constraints(clean_audit):
    s = clean_audit
    memo_id = _seed_memo(s)
    _insert_shadow(s, memo_id, "shadow-1")
    _insert_shadow(s, memo_id, "shadow-2")   # a NEW comparison may re-run it
    s.commit()

    # one challenger re-run per source memo per comparison
    with pytest.raises(IntegrityError):
        _insert_shadow(s, memo_id, "shadow-1")
    s.rollback()

    # a shadow row must point at a real production memo (FK)
    with pytest.raises(IntegrityError):
        _insert_shadow(s, "00000000-0000-0000-0000-000000000000", "shadow-3")
    s.rollback()

    # payload is the record — never nullable
    with pytest.raises(IntegrityError):
        s.execute(text(
            "INSERT INTO research.shadow_memos "
            "(source_memo_id, challenger_model, comparison_id, payload) "
            "VALUES (CAST(:m AS uuid), 'claude-sonnet-5', 'shadow-4', NULL)"),
            {"m": memo_id})
    s.rollback()
