"""Migration 0026 cycle + market.quarterly_fundamentals constraints.

Downgrade removes the table; upgrade restores it with the fact-store shape —
UNIQUE(instrument_id, fiscal_period_end), NOT NULL filing_date/fetched_at, the
anchorability CHECK (filing_date > fiscal_period_end — the probed vendor
defect of filing_date stamped at the period end must be structurally
unstorable), NULLable metric columns (missing is missing), and the append-only
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
                "SELECT to_regclass('market.quarterly_fundamentals')"
            )).scalar() is not None
    finally:
        engine.dispose()


def test_migration_0026_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _table_exists()
        _alembic("downgrade", "0025")
        assert not _table_exists()
        _alembic("upgrade", "head")
        assert _table_exists()
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _seeded_instrument(s) -> str:
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES ('ZGPA', 'XTEST', 'US', 'stock', 'Gpa Migration Corp', 'USD') "
        "RETURNING id")).scalar()


def _insert(s, iid, *, fpe, fd="2023-05-01", gp="10.00", tr="20.00",
            ta="100.00", cur="USD"):
    return s.execute(text(
        "INSERT INTO market.quarterly_fundamentals (instrument_id, "
        "fiscal_period_end, filing_date, gross_profit, total_revenue, "
        "total_assets, currency, source, fetched_at) "
        "VALUES (:iid, :fpe, :fd, :gp, :tr, :ta, :cur, 'test', :fa)"),
        {"iid": iid, "fpe": fpe, "fd": fd, "gp": gp, "tr": tr, "ta": ta,
         "cur": cur, "fa": FETCHED})


def test_key_is_unique_per_instrument_and_fiscal_period(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    _insert(s, iid, fpe="2023-03-31")
    with pytest.raises(IntegrityError):
        _insert(s, iid, fpe="2023-03-31", gp="99.00")


def test_append_only_do_nothing_never_overwrites(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    _insert(s, iid, fpe="2023-03-31", gp="10.00")
    # a second insert on the same natural key with ON CONFLICT DO NOTHING must
    # leave the original fact untouched (immutability of a settled statement)
    s.execute(text(
        "INSERT INTO market.quarterly_fundamentals (instrument_id, "
        "fiscal_period_end, filing_date, gross_profit, total_revenue, "
        "total_assets, currency, source, fetched_at) "
        "VALUES (:iid, '2023-03-31', '2023-05-01', '999.99', NULL, NULL, "
        "'USD', 'test', :fa) "
        "ON CONFLICT (instrument_id, fiscal_period_end) DO NOTHING"),
        {"iid": iid, "fa": FETCHED})
    kept = s.execute(text(
        "SELECT gross_profit FROM market.quarterly_fundamentals "
        "WHERE instrument_id = :iid AND fiscal_period_end = '2023-03-31'"),
        {"iid": iid}).scalar()
    assert float(kept) == 10.00


def test_anchorability_check_filing_after_period_end(pg_session):
    """The probed vendor defect (filing_date == period end) must be
    structurally unstorable — and so must a filing BEFORE the period end."""
    s = pg_session
    iid = _seeded_instrument(s)
    with pytest.raises(IntegrityError):
        _insert(s, iid, fpe="2023-03-31", fd="2023-03-31")   # degenerate
    s.rollback()
    iid = _seeded_instrument(s)
    with pytest.raises(IntegrityError):
        _insert(s, iid, fpe="2023-03-31", fd="2023-01-15")   # before period end
    s.rollback()
    iid = _seeded_instrument(s)
    _insert(s, iid, fpe="2023-03-31", fd="2023-04-01")       # strictly after: ok


def test_metric_columns_nullable_but_anchors_not_null(pg_session):
    s = pg_session
    iid = _seeded_instrument(s)
    # all-NULL metrics are storable (the INGEST refuses metricless quarters;
    # the schema's job is anchors, not policy)
    _insert(s, iid, fpe="2023-03-31", gp=None, tr=None, ta=None)
    with pytest.raises(IntegrityError):
        _insert(s, iid, fpe="2023-06-30", fd=None)           # filing_date NOT NULL
    s.rollback()
    iid = _seeded_instrument(s)
    with pytest.raises(IntegrityError):
        s.execute(text(
            "INSERT INTO market.quarterly_fundamentals (instrument_id, "
            "fiscal_period_end, filing_date, source, fetched_at) "
            "VALUES (:iid, '2023-03-31', '2023-05-01', 'test', NULL)"),
            {"iid": iid})                                    # fetched_at NOT NULL
