"""Migration 0035 cycle: upgrade adds 'research_shadow' to the strategies state
CHECK and a nullable shadowed_at column (the ADR-0018 independent-review
downgrade support); downgrade re-maps any research_shadow row to 'suspended',
drops shadowed_at, and restores the pre-0035 8-value CHECK verbatim. Follows the
0020 migration-test pattern."""
from __future__ import annotations

import importlib.util
import os
import subprocess

from sqlalchemy import create_engine, text

from tests.conftest import ROOT, URL, _ensure_test_db, requires_pg

pytestmark = requires_pg

_MIGRATION = ROOT / "migrations" / "versions" / "0035_research_shadow_state.py"


def _down_revision() -> str:
    spec = importlib.util.spec_from_file_location("mig_0035", _MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.down_revision)


def _alembic(*args: str) -> None:
    env = {**os.environ, "ATLAS_DATABASE_URL": URL}
    r = subprocess.run(["alembic", *args], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stderr}"


def _probe() -> tuple[str, bool]:
    """(strategies state CHECK def, shadowed_at column exists)."""
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            state_check = c.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'quant.strategies'::regclass "
                "  AND conname = 'strategies_state_check'")).scalar_one()
            shadowed_at = c.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'quant' AND table_name = 'strategies' "
                "  AND column_name = 'shadowed_at'")).scalar() == 1
            return str(state_check), shadowed_at
    finally:
        engine.dispose()


def test_migration_0035_downgrade_upgrade_cycle():
    _ensure_test_db()
    try:
        state_check, shadowed_at = _probe()
        assert "research_shadow" in state_check
        assert shadowed_at

        _alembic("downgrade", _down_revision())
        state_check, shadowed_at = _probe()
        assert "research_shadow" not in state_check   # pre-0035 CHECK restored
        assert "suspended" in state_check             # 0020 value survives
        assert not shadowed_at                        # column dropped

        _alembic("upgrade", "head")
        state_check, shadowed_at = _probe()
        assert "research_shadow" in state_check
        assert shadowed_at
    finally:
        _alembic("upgrade", "head")                   # never leave the DB below head
