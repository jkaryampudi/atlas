"""Migration 0027 cycle + reporting.attribution_daily constraints.

Downgrade removes the table AND the reporting schema; upgrade restores the
decomposition shape: UNIQUE(session_date, sleeve) — the idempotent-upsert
target — the CLOSED sleeve CHECK (a third sleeve is a signed schema change,
never a silent string), NOT NULL value_aud (an empty sleeve is A$0, a real
value), and NULLable return columns (a missing measurement is NULL, never 0).
"""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

CREATED = datetime(2026, 7, 16, 2, 0, tzinfo=UTC)


def _alembic(*args: str) -> None:
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(["alembic", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr}"


def _exists(what: str) -> bool:
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            if what == "table":
                return c.execute(text(
                    "SELECT to_regclass('reporting.attribution_daily')"
                )).scalar() is not None
            return c.execute(text(
                "SELECT 1 FROM information_schema.schemata "
                "WHERE schema_name = 'reporting'")).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0027_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _exists("table") and _exists("schema")
        _alembic("downgrade", "0026")
        assert not _exists("table") and not _exists("schema")
        _alembic("upgrade", "head")
        assert _exists("table") and _exists("schema")
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _insert(s, *, d="2026-07-13", sleeve="core", value="1500.00",
            ret=None, bench=None):
    return s.execute(text(
        "INSERT INTO reporting.attribution_daily (session_date, sleeve, "
        "value_aud, ret_1d, benchmark_ret_1d, created_at) "
        "VALUES (:d, :s, :v, :r, :b, :ca)"),
        {"d": d, "s": sleeve, "v": value, "r": ret, "b": bench, "ca": CREATED})


def test_one_row_per_session_and_sleeve(pg_session):
    s = pg_session
    s.execute(text("DELETE FROM reporting.attribution_daily"))
    _insert(s, sleeve="core")
    _insert(s, sleeve="cash", value="98500.00")     # same day, other sleeve: fine
    _insert(s, d="2026-07-14", sleeve="core")       # other day, same sleeve: fine
    with pytest.raises(IntegrityError):
        _insert(s, sleeve="core", value="9999.99")  # the upsert's conflict target


def test_sleeve_vocabulary_is_closed(pg_session):
    s = pg_session
    s.execute(text("DELETE FROM reporting.attribution_daily"))
    for ok in ("core", "xsmom", "pead", "cash", "total"):
        _insert(s, sleeve=ok, value="0.00")
    with pytest.raises(IntegrityError):
        _insert(s, d="2026-07-14", sleeve="satellite", value="0.00")


def test_value_required_returns_nullable(pg_session):
    s = pg_session
    s.execute(text("DELETE FROM reporting.attribution_daily"))
    _insert(s, ret="0.10000000", bench="0.01428571")   # both present: fine
    _insert(s, d="2026-07-14")                         # both NULL: day one
    with pytest.raises(IntegrityError):
        _insert(s, d="2026-07-15", value=None)         # A$0 is a value; NULL is not
