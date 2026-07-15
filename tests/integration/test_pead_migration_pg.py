"""Migration 0021 cycle + market.earnings_surprises constraints.

Downgrade removes the table; upgrade restores it with the fact-store shape —
UNIQUE(instrument_id, fiscal_period_end), NOT NULL eps/report_date/fetched_at,
the closed-vocabulary CHECK on before_after_market, and the append-only
immutability the ingest relies on (ON CONFLICT DO NOTHING never overwrites)."""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

FETCHED = datetime(2026, 7, 15, 2, 0, tzinfo=UTC)


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
                "SELECT to_regclass('market.earnings_surprises')")).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0021_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _table_exists()
        _alembic("downgrade", "0020")
        assert not _table_exists()
        _alembic("upgrade", "head")
        assert _table_exists()
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _seeded_instrument(s) -> str:
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES ('ZSUE', 'XTEST', 'US', 'stock', 'Surprise Migration Corp', 'USD') "
        "RETURNING id")).scalar()


def _insert(s, iid, *, fpe, rd="2023-05-01", a="1.10", e="1.00",
            sp="10.0", cur="USD", baf="BeforeMarket"):
    return s.execute(text(
        "INSERT INTO market.earnings_surprises (instrument_id, fiscal_period_end, "
        "report_date, eps_actual, eps_estimate, surprise_pct, currency, "
        "before_after_market, source, fetched_at) "
        "VALUES (:iid, :fpe, :rd, :a, :e, :sp, :cur, :baf, 'test', :fa)"),
        {"iid": iid, "fpe": fpe, "rd": rd, "a": a, "e": e, "sp": sp,
         "cur": cur, "baf": baf, "fa": FETCHED})


def test_key_is_unique_per_instrument_and_fiscal_period(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    _insert(s, iid, fpe="2023-03-31")
    with pytest.raises(IntegrityError):
        _insert(s, iid, fpe="2023-03-31", a="2.00")


def test_append_only_do_nothing_never_overwrites(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    _insert(s, iid, fpe="2023-03-31", a="1.10")
    # a second insert on the same natural key with ON CONFLICT DO NOTHING must
    # leave the original fact untouched (immutability of a settled report)
    s.execute(text(
        "INSERT INTO market.earnings_surprises (instrument_id, fiscal_period_end, "
        "report_date, eps_actual, eps_estimate, surprise_pct, currency, "
        "before_after_market, source, fetched_at) "
        "VALUES (:iid, '2023-03-31', '2023-05-01', '9.99', '1.00', NULL, 'USD', "
        "'BeforeMarket', 'test', :fa) "
        "ON CONFLICT (instrument_id, fiscal_period_end) DO NOTHING"),
        {"iid": iid, "fa": FETCHED})
    kept = s.execute(text(
        "SELECT eps_actual FROM market.earnings_surprises "
        "WHERE instrument_id = :iid AND fiscal_period_end = '2023-03-31'"),
        {"iid": iid}).scalar()
    assert float(kept) == 1.10


def test_before_after_market_closed_vocabulary(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    _insert(s, iid, fpe="2023-03-31", baf="AfterMarket")   # ok
    _insert(s, iid, fpe="2023-06-30", baf=None)            # NULL is a valid answer
    with pytest.raises(IntegrityError):                    # free text refused
        _insert(s, iid, fpe="2023-09-30", baf="whenever-o-clock")


def test_required_columns_not_null(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    with pytest.raises(IntegrityError):
        s.execute(text(
            "INSERT INTO market.earnings_surprises (instrument_id, "
            "fiscal_period_end, report_date, eps_actual, eps_estimate, source, "
            "fetched_at) VALUES (:iid, '2023-03-31', '2023-05-01', '1.1', '1.0', "
            "'test', NULL)"), {"iid": iid})   # fetched_at NOT NULL
