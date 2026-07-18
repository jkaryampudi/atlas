"""quant.trial_registry.lineage — lineage-scoped trial counting (ADR-0016,
board item 9: "The counting defect").

WHY: the deflated-Sharpe gate counted trials PER FAMILY NAME (ADR-0002
convention), so a freshly-named variant (e.g. xsmom-impl500-tr) always
evaluated at n_trials=1 — the multiple-testing penalty could not bind on any
first-in-family run. Renaming a variant must never reset its penalty: the
gate now deflates at the trial count of the strategy's full LINEAGE.

This migration adds a nullable `lineage` tag and backfills EXISTING rows by
the prefix mapping below (verbatim, per the Principal's ADR-0016 decision):

    strategy_family = 'momentum' OR LIKE 'xsmom%'  -> 'momentum'
    strategy_family LIKE 'combined-impl%'          -> 'momentum+pead'
    strategy_family LIKE 'pead%'                   -> 'pead'
    strategy_family LIKE 'quality%'                -> 'quality'
    strategy_family LIKE 'trend%'                  -> 'trend'
    strategy_family LIKE 'meanrev%'                -> 'meanrev'
    strategy_family LIKE 'breakout%'               -> 'breakout'
    strategy_family LIKE 'fxlab-%'                 -> 'fxlab'
    anything else                                  -> NULL (unknown/legacy)

The combined-impl choice, documented honestly: the combined satellite mixes
BOTH parents (momentum + PEAD). Making a combined book's trials count against
both parent lineages going forward is complex; v1 records 'momentum+pead' as
its OWN lineage, and lineage_count('momentum') does NOT include it. This is a
known conservative simplification, stated here rather than hidden.

NULL stays allowed at the schema level for unknown/legacy rows; every NEW
registration REQUIRES a lineage — enforced in code (registry.register_trial),
which is where all inserts happen. Counting is forward-only: old reports and
verdicts are history and untouched (ADR-0016: no retroactive re-judgment).

Revision ID: 0032
"""
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

# The exact backfill applied to existing rows. Kept as a module constant so
# the golden test (tests/integration/test_trial_lineage_pg.py) executes THIS
# text against legacy-shaped rows — the mapping under test is the mapping
# that ran, not a copy that can drift.
BACKFILL_SQL = """
UPDATE quant.trial_registry SET lineage = CASE
  WHEN strategy_family = 'momentum'
       OR strategy_family LIKE 'xsmom%'         THEN 'momentum'
  WHEN strategy_family LIKE 'combined-impl%'    THEN 'momentum+pead'
  WHEN strategy_family LIKE 'pead%'             THEN 'pead'
  WHEN strategy_family LIKE 'quality%'          THEN 'quality'
  WHEN strategy_family LIKE 'trend%'            THEN 'trend'
  WHEN strategy_family LIKE 'meanrev%'          THEN 'meanrev'
  WHEN strategy_family LIKE 'breakout%'         THEN 'breakout'
  WHEN strategy_family LIKE 'fxlab-%'           THEN 'fxlab'
  ELSE NULL
END
WHERE lineage IS NULL
"""


def upgrade() -> None:
    op.execute("ALTER TABLE quant.trial_registry ADD COLUMN lineage text")
    op.execute(BACKFILL_SQL)
    op.execute("CREATE INDEX idx_trial_registry_lineage "
               "ON quant.trial_registry (lineage)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.idx_trial_registry_lineage")
    op.execute("ALTER TABLE quant.trial_registry DROP COLUMN lineage")
