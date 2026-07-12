"""Migration 0014 cycle: downgrade removes fxlab.bars_daily AND the fxlab
schema, upgrade restores them; PK(pair, bar_date) and NOT NULL OHLC are
structural; the ADR-0008 seal is verified AT THE DATABASE — atlas_agent_reader
must hold no privilege on the sandbox schema or table. Also exercises the
idempotent ingest upsert (ON CONFLICT DO NOTHING: stored vendor bars are
facts, never rewritten) against the real table."""
from __future__ import annotations

import os
import subprocess
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from atlas.fxlab.engine import FxBar
from atlas.fxlab.ingest import upsert_bars
from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg


def _alembic(*args: str) -> None:
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(["alembic", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr}"


def _scalar(sql: str):
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            return c.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def test_migration_0014_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _scalar("SELECT to_regclass('fxlab.bars_daily')") is not None
        _alembic("downgrade", "0013")
        assert _scalar("SELECT to_regclass('fxlab.bars_daily')") is None
        assert _scalar("SELECT count(*) FROM pg_namespace "
                       "WHERE nspname = 'fxlab'") == 0    # schema gone too
        _alembic("upgrade", "head")
        assert _scalar("SELECT to_regclass('fxlab.bars_daily')") is not None
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def test_seal_no_agent_reader_privileges():
    """ADR-0008 §3 at the database: the reasoning plane's read role must have
    NOTHING on fxlab — no schema usage, no table select (contrast: market.*
    is granted in 0001)."""
    _ensure_test_db()
    assert _scalar("SELECT has_schema_privilege('atlas_agent_reader', "
                   "'fxlab', 'USAGE')") is False
    assert _scalar("SELECT has_table_privilege('atlas_agent_reader', "
                   "'fxlab.bars_daily', 'SELECT')") is False


def _bar(day: date, close: float) -> FxBar:
    return FxBar(bar_date=day, open=close, high=close, low=close, close=close)


def test_pk_refuses_duplicate_pair_date(pg_session):
    s = pg_session
    ins = text("INSERT INTO fxlab.bars_daily (pair, bar_date, open, high, low, close, source) "
               "VALUES ('EURUSD', '2026-07-06', 1.08, 1.09, 1.07, 1.085, 't')")
    s.execute(ins)
    with pytest.raises(IntegrityError):
        s.execute(ins)


def test_ohlc_not_nullable(pg_session):
    with pytest.raises(IntegrityError):
        pg_session.execute(text(
            "INSERT INTO fxlab.bars_daily (pair, bar_date, open, high, low, close, source) "
            "VALUES ('EURUSD', '2026-07-06', 1.08, 1.09, 1.07, NULL, 't')"))


def test_upsert_is_idempotent_and_never_rewrites(pg_session):
    """Second ingest of the same bar (even with a different price) is a
    no-op: a stored vendor bar is a recorded fact."""
    s = pg_session
    day = date(2026, 7, 7)
    assert upsert_bars(s, [_bar(day, 1.0850)], source="test") == 1
    assert upsert_bars(s, [_bar(day, 9.9999)], source="test") == 0
    kept = s.execute(text(
        "SELECT close FROM fxlab.bars_daily WHERE pair='EURUSD' AND bar_date=:d"),
        {"d": day}).scalar()
    assert float(kept) == 1.0850
