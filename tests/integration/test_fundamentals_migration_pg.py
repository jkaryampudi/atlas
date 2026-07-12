"""Migration 0012 cycle: downgrade removes market.fundamentals, upgrade
restores it with the append-only snapshot shape — UNIQUE(instrument_id, as_of)
and NOT NULL payload are structural, not application convention."""
from __future__ import annotations

import json
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
                "SELECT to_regclass('market.fundamentals')")).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0012_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _table_exists()          # at head, the table is there
        _alembic("downgrade", "0011")
        assert not _table_exists()      # clean removal
        _alembic("upgrade", "head")
        assert _table_exists()          # clean re-creation
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _seeded_instrument(s) -> str:
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES ('ZMIG', 'XTEST', 'US', 'stock', 'Migration Test Corp', 'USD') "
        "RETURNING id")).scalar()


def test_snapshot_key_is_unique_per_instrument_and_day(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    ins = text("INSERT INTO market.fundamentals (instrument_id, as_of, payload, "
               "source) VALUES (:iid, '2026-07-10', CAST(:p AS jsonb), 'test')")
    s.execute(ins, {"iid": iid, "p": json.dumps({"General": {}})})
    with pytest.raises(IntegrityError):
        s.execute(ins, {"iid": iid, "p": json.dumps({"General": {"n": 2}})})


def test_payload_is_not_nullable(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    with pytest.raises(IntegrityError):
        s.execute(text(
            "INSERT INTO market.fundamentals (instrument_id, as_of, payload, "
            "source) VALUES (:iid, '2026-07-10', NULL, 'test')"), {"iid": iid})
