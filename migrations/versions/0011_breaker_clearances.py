"""risk.breaker_clearances — the dual-confirmation human action that clears a
latched DD2/DD3 drawdown breaker.

Doc 04 §5 lineage: "Resumption from DD2/DD3 requires the dual-confirmation
human action" and "Breaker state changes are audit events and cannot be
cleared by agents". Until this table shipped, atlas.dcp.trading.proposals
folded the breaker with human_cleared always False, so DD2/DD3 latched
forever (fail-closed by design). A confirmed row here is the fold's ONLY
source of human_cleared=True — and clearing steps the latch down to the
COMPUTED target for the live drawdown, so a clearance during a still-deep
drawdown leaves DD2 in force: you can clear a latched memory of a drawdown,
never a live one.

Dual-confirmation shape follows the risk.limit_sets precedent (0001):
requested_at is confirmation A, confirmed_at is confirmation B (NULL =
pending), and the ≥1h gap (Doc 06 §2: "second confirmation (≥1h later,
enforced)"; error code DUAL_CONFIRM_TOO_SOON, Doc 06 §3.3) is a structural
CHECK, not just application code — a too-soon confirmation is unrepresentable
even by a hand-written UPDATE.

Revision ID: 0011
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE risk.breaker_clearances (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      from_level text NOT NULL CHECK (from_level IN ('DD2','DD3')),
      reason text NOT NULL,
      requested_by text NOT NULL,
      requested_at timestamptz NOT NULL,          -- confirmation A
      confirmed_at timestamptz,                   -- confirmation B; NULL = pending
      created_at timestamptz NOT NULL DEFAULT now(),
      -- structural ≥1h dual-confirmation gap (risk.limit_sets precedent)
      CONSTRAINT breaker_clearance_dual_confirm_gap CHECK (
        confirmed_at IS NULL OR confirmed_at >= requested_at + interval '1 hour')
    );

    -- hot lookups: the (at most one) pending request, and confirmed instants
    -- in fold order
    CREATE INDEX breaker_clearances_pending_idx
      ON risk.breaker_clearances(requested_at) WHERE confirmed_at IS NULL;
    CREATE INDEX breaker_clearances_confirmed_idx
      ON risk.breaker_clearances(confirmed_at) WHERE confirmed_at IS NOT NULL;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS risk.breaker_clearances;")
