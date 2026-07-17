"""Migration 0024 cycle + feature-store constraints (ADR-0011 step 1).

Downgrade removes the two tables and the trial-registry provenance columns;
upgrade restores them with the point-in-time shape: UNIQUE feature name, the
natural key (feature, instrument, session_date, dataset_version), NOT NULL
value/computed_at, and nullable provenance columns so existing trial rows
honestly stay NULL."""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

COMPUTED = datetime(2026, 7, 17, 2, 0, tzinfo=UTC)


def _alembic(*args: str) -> None:
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(["alembic", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr}"


def _exists(rel: str) -> bool:
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            return c.execute(text("SELECT to_regclass(:r)"),
                             {"r": rel}).scalar() is not None
    finally:
        engine.dispose()


def _trial_columns() -> set[str]:
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            return {r.column_name for r in c.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'quant' "
                "  AND table_name = 'trial_registry'"))}
    finally:
        engine.dispose()


def test_migration_0024_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        assert _exists("quant.feature_definitions")
        assert _exists("quant.feature_values")
        assert {"hypothesis", "dataset_version"} <= _trial_columns()
        _alembic("downgrade", "0023")
        assert not _exists("quant.feature_definitions")
        assert not _exists("quant.feature_values")
        assert not ({"hypothesis", "dataset_version"} & _trial_columns())
        _alembic("upgrade", "head")
        assert _exists("quant.feature_values")
        assert {"hypothesis", "dataset_version"} <= _trial_columns()
    finally:
        _alembic("upgrade", "head")     # never leave the test DB below head


def _definition(s, name="feat_mig_test") -> str:
    return s.execute(text(
        "INSERT INTO quant.feature_definitions "
        "(name, version, spec, code_sha, created_at) "
        "VALUES (:n, '1.0.0', '{}', 'deadbeef', :ca) RETURNING id"),
        {"n": name, "ca": COMPUTED}).scalar()


def _instrument(s, sym="ZFSM") -> str:
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES (:s, 'XTEST', 'US', 'stock', 'Feature Migration Corp', 'USD') "
        "RETURNING id"), {"s": sym}).scalar()


def test_feature_name_is_unique(pg_session):
    s = pg_session
    _definition(s)
    with pytest.raises(IntegrityError):
        _definition(s)


def test_feature_values_natural_key_unique(pg_session):
    s = pg_session
    fid, iid = _definition(s), _instrument(s)
    ins = text(
        "INSERT INTO quant.feature_values (feature_id, instrument_id, "
        "session_date, value, dataset_version, computed_at) "
        "VALUES (:f, :i, '2025-05-30', :v, 'dsv1', :ca)")
    s.execute(ins, {"f": fid, "i": iid, "v": "0.5", "ca": COMPUTED})
    # same natural key under a NEW dataset_version is a new fact — allowed
    s.execute(text(
        "INSERT INTO quant.feature_values (feature_id, instrument_id, "
        "session_date, value, dataset_version, computed_at) "
        "VALUES (:f, :i, '2025-05-30', :v, 'dsv2', :ca)"),
        {"f": fid, "i": iid, "v": "0.6", "ca": COMPUTED})
    with pytest.raises(IntegrityError):     # same vintage: refused
        s.execute(ins, {"f": fid, "i": iid, "v": "0.7", "ca": COMPUTED})


def test_feature_value_and_computed_at_not_null(pg_session):
    s = pg_session
    fid, iid = _definition(s), _instrument(s)
    with pytest.raises(IntegrityError):
        s.execute(text(
            "INSERT INTO quant.feature_values (feature_id, instrument_id, "
            "session_date, value, dataset_version, computed_at) "
            "VALUES (:f, :i, '2025-05-30', NULL, 'dsv1', :ca)"),
            {"f": fid, "i": iid, "ca": COMPUTED})


def test_trial_registry_provenance_defaults_null(pg_session):
    """The 0.1 gap-fill is additive: a row inserted through the ORIGINAL
    column list (existing history's shape) carries NULL provenance."""
    s = pg_session
    rid = s.execute(text(
        "INSERT INTO quant.trial_registry (strategy_family, spec_hash, metrics) "
        "VALUES ('mig-test', 'abc', '{}') RETURNING id")).scalar()
    row = s.execute(text(
        "SELECT hypothesis, dataset_version FROM quant.trial_registry "
        "WHERE id = :r"), {"r": rid}).one()
    assert row.hypothesis is None and row.dataset_version is None
