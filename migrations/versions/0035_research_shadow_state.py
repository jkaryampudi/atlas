"""quant.strategies gains the 'research_shadow' state + a shadowed_at stamp —
the lifecycle support for the independent-review downgrade (ADR-0018).

The 2026-07-20 independent review (REVIEW_PACKAGE/) returned "REJECT STRATEGY
EVIDENCE" for the *executable* xsmom-pit-tr sleeve: the deployed generator ranks
split-adjusted PRICE return while approval validated TOTAL return, the honest
lineage-count DSR (~0.85, ADR-0016) is below the 0.90 bar, and the run is not
reproducible from an immutable code+data snapshot. Phase P0 downgrades the
strategy from operational 'paper' to a NON-AUTHORITATIVE 'research_shadow'
status: it deploys no paper capital and its performance is never reported as
validated, but its identity and history are preserved for observation.

'research_shadow' is added to the SAME named CHECK constraint that migration
0004 created and migration 0020 extended (for 'suspended'); the drop/re-create
pattern mirrors 0020 exactly. It is deliberately EXCLUDED from every tradability
gate (state IN ('paper','live')) so a downgraded strategy stops deploying
capital by construction — no strategy math, parameter, or sizing formula
changes.

shadowed_at (nullable timestamptz) records WHEN the strategy was moved to
research_shadow (injected clock, never now()). The fail-closed promotion guard
(atlas/dcp/backtest/approval.py) requires a SIGNED validation artifact
(quant.validation_reports verdict='approve') created strictly AFTER shadowed_at
before a research_shadow strategy may return to 'paper' — the stale pre-downgrade
approval can never be reused (ADR-0018 fail-closed rule). NULL for every strategy
that was never shadowed; historical rows honestly stay NULL.

Revision ID: 0035
"""
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

# The 8 values live after migration 0020; research_shadow is the 9th.
_STATES_OLD = ("'draft','backtested','validated','approved','live','paper',"
               "'retired','suspended'")
_STATES_NEW = _STATES_OLD + ",'research_shadow'"


def upgrade() -> None:
    op.execute(f"""
    ALTER TABLE quant.strategies DROP CONSTRAINT strategies_state_check;
    ALTER TABLE quant.strategies ADD CONSTRAINT strategies_state_check
      CHECK (state IN ({_STATES_NEW}));

    ALTER TABLE quant.strategies ADD COLUMN IF NOT EXISTS shadowed_at timestamptz;
    """)


def downgrade() -> None:
    # No row may reference the value being removed, or the re-created CHECK fails.
    op.execute(f"""
    UPDATE quant.strategies SET state = 'suspended', shadowed_at = NULL
      WHERE state = 'research_shadow';
    ALTER TABLE quant.strategies DROP COLUMN IF EXISTS shadowed_at;
    ALTER TABLE quant.strategies DROP CONSTRAINT strategies_state_check;
    ALTER TABLE quant.strategies ADD CONSTRAINT strategies_state_check
      CHECK (state IN ({_STATES_OLD}));
    """)
