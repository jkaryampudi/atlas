"""Migration 0020 cycle: downgrade removes quant.signals + quant.sleeve_daily
and restores the pre-'suspended' strategy state CHECK; upgrade recreates the
tables with the structural shape — long-only direction CHECK, the natural
UNIQUE keys, NOT NULL clock-stamped created_at — and re-admits 'suspended'
(the ADR-0010 latching demotion target). Follows the 0019 test pattern."""
from __future__ import annotations

import importlib.util
import os
import subprocess

from sqlalchemy import create_engine, text

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

_MIGRATION = ROOT / "migrations" / "versions" / "0020_quant_signals.py"


def _down_revision() -> str:
    spec = importlib.util.spec_from_file_location("mig_0020", _MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.down_revision)


def _alembic(*args: str) -> None:
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(["alembic", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr}"


def _probe() -> tuple[bool, bool, str, bool]:
    """(signals exists, sleeve_daily exists, strategies state CHECK def,
    direction CHECK exists)."""
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            signals = c.execute(text(
                "SELECT to_regclass('quant.signals')")).scalar() is not None
            sleeve = c.execute(text(
                "SELECT to_regclass('quant.sleeve_daily')")).scalar() is not None
            state_check = c.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'quant.strategies'::regclass "
                "  AND conname = 'strategies_state_check'")).scalar_one()
            direction = bool(signals) and c.execute(text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'quant.signals'::regclass "
                "  AND conname = 'signals_direction_check'")).scalar() == 1
            return signals, sleeve, str(state_check), direction
    finally:
        engine.dispose()


def test_migration_0020_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        signals, sleeve, state_check, direction = _probe()
        assert signals and sleeve and direction
        assert "suspended" in state_check

        _alembic("downgrade", _down_revision())
        signals, sleeve, state_check, _ = _probe()
        assert not signals and not sleeve
        assert "suspended" not in state_check     # 0004 CHECK restored verbatim

        _alembic("upgrade", "head")
        signals, sleeve, state_check, direction = _probe()
        assert signals and sleeve and direction
        assert "suspended" in state_check
    finally:
        _alembic("upgrade", "head")               # never leave the DB below head


def test_natural_key_and_direction_are_structural(pg_session):
    """UNIQUE(strategy, instrument, signal_date) and CHECK(direction='long')
    are table facts, not application convention."""
    s = pg_session
    uniq = s.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'quant.signals'::regclass AND contype = 'u'")).scalar_one()
    assert uniq == "UNIQUE (strategy_id, instrument_id, signal_date)"
    sleeve_uniq = s.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'quant.sleeve_daily'::regclass "
        "  AND contype = 'u'")).scalar_one()
    assert sleeve_uniq == "UNIQUE (strategy_id, session_date)"
    direction = s.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'quant.signals'::regclass "
        "  AND conname = 'signals_direction_check'")).scalar_one()
    assert direction == "CHECK ((direction = 'long'::text))"
