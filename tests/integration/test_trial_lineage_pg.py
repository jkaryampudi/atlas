"""Lineage-scoped trial counting (ADR-0016, board item 9 — the counting
defect): migration 0032 golden backfill mapping, schema shape, and the runner
smoke proving deflated Sharpe now computes at the LINEAGE count even when the
family is brand new (the exact hole: a freshly-named variant at n_trials=1).

The backfill golden executes the MIGRATION MODULE'S OWN BACKFILL_SQL against
legacy-shaped rows (inserted raw, lineage NULL, inside the rolled-back test
transaction) — the mapping under test is the text that ran on dev, not a
copy that can drift. The row distribution below replicates dev's 43 rows at
the time 0032 was written."""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.registry import lineage_count, register_trial, trial_count
from atlas.dcp.backtest.validation import deflated_sharpe
from atlas.dcp.backtest.xsmom_run import run_xsmom
from tests.conftest import requires_pg
from tests.integration.test_xsmom_run_pg import _seed as seed_xsmom_world

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations" / "versions" / "0032_trial_lineage.py"

# Dev's quant.trial_registry family distribution when 0032 was written:
# 24 distinct families, 43 rows.
DEV_SHAPED_ROWS: dict[str, int] = {
    "momentum": 7, "xsmom": 1, "xsmom-etf": 1, "xsmom-pit": 1,
    "xsmom-pit-tr": 2, "xsmom-pit-tr-2016": 1, "xsmom-impl-tr": 1,
    "xsmom-impl-tr-2016": 1, "xsmom-impl500-tr": 1, "xsmom-impl500-tr-2016": 1,
    "pead-sue-tr": 3, "pead-sue-tr-2016": 2, "pead-impl-tr": 1,
    "pead-impl-tr-2016": 1,
    "quality-gpa-tr": 1, "quality-gpa-tr-2016": 1,
    "trend": 4, "meanrev": 4, "breakout": 4,
    "fxlab-donchian": 1, "fxlab-ma_cross": 1, "fxlab-rsi_fade": 1,
    "combined-impl-tr": 1, "combined-impl-tr-2016": 1,
}

# The golden verdict of the ADR-0016 prefix mapping over those rows. Note the
# documented conservative simplification: combined-impl% is its OWN lineage
# ('momentum+pead'); lineage_count('momentum') does NOT include it.
EXPECTED_LINEAGE_COUNTS: dict[str, int] = {
    "momentum": 17, "pead": 7, "quality": 2, "trend": 4, "meanrev": 4,
    "breakout": 4, "fxlab": 3, "momentum+pead": 2,
}


def _load_migration_0032():
    spec = importlib.util.spec_from_file_location("migration_0032", MIGRATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _insert_legacy_row(s, family: str, i: int) -> None:
    """Legacy-shaped row: pre-0032 inserts carried no lineage."""
    s.execute(text(
        "INSERT INTO quant.trial_registry (strategy_family, spec_hash, metrics) "
        "VALUES (:f, :h, '{}')"), {"f": family, "h": f"legacy-{family}-{i}"})


def test_migration_0032_chains_to_0031():
    mod = _load_migration_0032()
    assert mod.revision == "0032"
    assert mod.down_revision == "0031"


def test_lineage_column_is_nullable_text(pg_session):
    """NULL stays allowed at the schema level (unknown/legacy rows); the
    every-new-registration requirement lives in register_trial."""
    row = pg_session.execute(text(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema='quant' AND table_name='trial_registry' "
        "AND column_name='lineage'")).one()
    assert row.data_type == "text"
    assert row.is_nullable == "YES"


def test_backfill_mapping_golden(pg_session):
    """The migration's own BACKFILL_SQL over a replica of dev's 43 rows must
    produce exactly the ADR-0016 mapping — and leave unknown families NULL."""
    s = pg_session
    s.execute(text("TRUNCATE quant.trial_registry"))
    for family, n in DEV_SHAPED_ROWS.items():
        for i in range(n):
            _insert_legacy_row(s, family, i)
    _insert_legacy_row(s, "mystery-legacy", 0)      # unknown: must stay NULL

    s.execute(text(_load_migration_0032().BACKFILL_SQL))

    got = dict(s.execute(text(
        "SELECT lineage, count(*) FROM quant.trial_registry "
        "WHERE lineage IS NOT NULL GROUP BY lineage")).all())
    assert got == EXPECTED_LINEAGE_COUNTS
    assert sum(got.values()) == 43
    assert s.execute(text(
        "SELECT strategy_family FROM quant.trial_registry "
        "WHERE lineage IS NULL")).scalar_one() == "mystery-legacy"
    # and the counting defect is closed for the freshly-named variant:
    assert trial_count(s, "xsmom-impl500-tr") == 1
    assert lineage_count(s, "momentum") == 17


def test_backfill_is_idempotent_and_preserves_existing_tags(pg_session):
    """WHERE lineage IS NULL: re-running the backfill never rewrites a row
    that already carries a tag (e.g. one registered by new code mid-deploy)."""
    s = pg_session
    s.execute(text("TRUNCATE quant.trial_registry"))
    register_trial(s, family="xsmom-custom", lineage="special-tag",
                   spec={"v": 1}, metrics={})
    s.execute(text(_load_migration_0032().BACKFILL_SQL))
    assert s.execute(text(
        "SELECT lineage FROM quant.trial_registry "
        "WHERE strategy_family='xsmom-custom'")).scalar_one() == "special-tag"


def test_runner_dsr_computes_at_lineage_count_not_family_count(pg_session):
    """The board-item-9 smoke: three PRIOR momentum-lineage trials sit under
    OTHER family names; the first-ever 'xsmom'-family run must deflate at
    n_trials=4 (lineage), not 1 (family). Before ADR-0016 this exact run
    evaluated at n_trials=1 — the penalty could not bind on any
    first-in-family run."""
    s = pg_session
    seed_xsmom_world(s)                 # clears the momentum lineage + panel
    for fam in ("momentum", "xsmom-pit-tr", "xsmom-impl500-tr"):
        register_trial(s, family=fam, lineage="momentum",
                       spec={"fam": fam}, metrics={})
    audit = PostgresAuditLog(
        s, FrozenClock(datetime(2025, 6, 30, 22, tzinfo=UTC)))

    run = run_xsmom(s, audit, paths=10, seed=7)

    assert trial_count(s, "xsmom") == 1             # first-in-family ...
    assert run.n_trials == 4                        # ... not first-in-lineage
    assert run.gate.n_trials == 4
    assert run.lineage == "momentum"
    n_days = len(run.result.equity_curve) - 1
    assert run.gate.dsr == deflated_sharpe(run.result.sharpe, n_days, 4)
    # the penalty never inflates, and it BITES wherever the Sharpe is not
    # saturated (this fixture's smooth-drift Sharpe rounds both sides to 1.0
    # in float, so the bite is shown at a modest Sharpe over the SAME window
    # and the same lineage-vs-family counts):
    assert run.gate.dsr <= deflated_sharpe(run.result.sharpe, n_days, 1)
    assert deflated_sharpe(1.2, n_days, 4) < deflated_sharpe(1.2, n_days, 1)
