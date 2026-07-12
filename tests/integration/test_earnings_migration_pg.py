"""Migration 0018 cycle: downgrade removes market.earnings_calendar, upgrade
restores it with the calendar shape — UNIQUE(instrument_id, report_date),
NOT NULL fetched_at (injected clock, never DB now()), nullable when_time."""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

FETCHED = datetime(2026, 7, 13, 2, 0, tzinfo=UTC)


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
                "SELECT to_regclass('market.earnings_calendar')")).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0018_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _table_exists()          # at head, the table is there
        _alembic("downgrade", "0017")
        assert not _table_exists()      # clean removal
        _alembic("upgrade", "head")
        assert _table_exists()          # clean re-creation
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _seeded_instrument(s) -> str:
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES ('ZMIE', 'XTEST', 'US', 'stock', 'Earnings Migration Corp', 'USD') "
        "RETURNING id")).scalar()


def test_calendar_key_is_unique_per_instrument_and_report_date(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    ins = text("INSERT INTO market.earnings_calendar (instrument_id, report_date, "
               "when_time, fetched_at, source) "
               "VALUES (:iid, '2026-07-24', :w, :fa, 'test')")
    s.execute(ins, {"iid": iid, "w": "AfterMarket", "fa": FETCHED})
    with pytest.raises(IntegrityError):
        s.execute(ins, {"iid": iid, "w": None, "fa": FETCHED})


def test_fetched_at_is_not_nullable_and_when_time_is(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    s.execute(text(
        "INSERT INTO market.earnings_calendar (instrument_id, report_date, "
        "when_time, fetched_at, source) "
        "VALUES (:iid, '2026-07-24', NULL, :fa, 'test')"),
        {"iid": iid, "fa": FETCHED})    # when_time NULL is a valid vendor answer
    with pytest.raises(IntegrityError):
        s.execute(text(
            "INSERT INTO market.earnings_calendar (instrument_id, report_date, "
            "when_time, fetched_at, source) "
            "VALUES (:iid, '2026-08-24', NULL, NULL, 'test')"), {"iid": iid})
