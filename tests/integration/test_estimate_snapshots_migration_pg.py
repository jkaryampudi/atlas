"""Migration 0028 cycle: downgrade removes market.estimate_snapshots, upgrade
restores it with the archive shape — UNIQUE(instrument_id, fiscal_period_end,
snapshot_date), NOT NULL fetched_at (injected clock, never DB now()), every
metric column nullable (NULL = the vendor's genuine absence, never a zero)."""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

FETCHED = datetime(2026, 7, 17, 2, 0, tzinfo=UTC)


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
                "SELECT to_regclass('market.estimate_snapshots')")).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0028_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _table_exists()          # at head, the table is there
        _alembic("downgrade", "0027")
        assert not _table_exists()      # clean removal
        _alembic("upgrade", "head")
        assert _table_exists()          # clean re-creation
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _seeded_instrument(s) -> str:
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES ('ZMES', 'XTEST', 'US', 'stock', 'Estimate Migration Corp', 'USD') "
        "RETURNING id")).scalar()


INSERT = text(
    "INSERT INTO market.estimate_snapshots "
    "(instrument_id, fiscal_period_end, snapshot_date, eps_estimate_avg, "
    " source, fetched_at) "
    "VALUES (:iid, :fpe, :sd, :avg, 'test', :fa)")


def test_natural_key_refuses_same_period_same_session_twice(pg_session):
    """The append-only backstop: one row per (instrument, period, session).
    The writer's ON CONFLICT DO NOTHING leans on exactly this constraint."""
    s = pg_session
    iid = _seeded_instrument(s)
    s.execute(INSERT, {"iid": iid, "fpe": "2026-09-30", "sd": "2026-07-17",
                       "avg": "1.94", "fa": FETCHED})
    with pytest.raises(IntegrityError):
        s.execute(INSERT, {"iid": iid, "fpe": "2026-09-30", "sd": "2026-07-17",
                           "avg": "1.95", "fa": FETCHED})


def test_new_session_is_a_new_row_and_metrics_are_nullable(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    # same fiscal period, two sessions: both rows live side by side (the PIT
    # archive's whole purpose), and an all-NULL-metrics row is admissible at
    # the schema level (NULL = vendor absence)
    s.execute(INSERT, {"iid": iid, "fpe": "2026-09-30", "sd": "2026-07-17",
                       "avg": "1.94", "fa": FETCHED})
    s.execute(INSERT, {"iid": iid, "fpe": "2026-09-30", "sd": "2026-07-18",
                       "avg": None, "fa": FETCHED})
    n = s.execute(text(
        "SELECT count(*) FROM market.estimate_snapshots "
        "WHERE instrument_id = :iid AND fiscal_period_end = '2026-09-30'"),
        {"iid": iid}).scalar()
    assert n == 2


def test_fetched_at_is_not_nullable(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    with pytest.raises(IntegrityError):
        s.execute(INSERT, {"iid": iid, "fpe": "2026-09-30", "sd": "2026-07-17",
                           "avg": "1.94", "fa": None})
