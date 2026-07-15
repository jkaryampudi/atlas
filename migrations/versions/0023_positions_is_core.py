"""trading.positions.is_core — mark a settled position as a legitimate passive
index-core holding, so the risk engine's stop-based L7 (ADR-0014) can zero its
open-risk contribution WITHOUT ever zeroing a satellite whose stop was dropped.

WHY THIS MIGRATION EXISTS
ADR-0012 deploys a passive index core that is REBALANCED, NOT STOPPED: a core
position carries no stop (current_stop NULL). Under the pre-ADR-0014 L7 rule a
stopless position fails closed and counts its FULL value as aggregate open risk
(atlas/dcp/risk/engine.py + the book-builder in atlas/dcp/trading/proposals.py).
That is correct for a satellite — a satellite MUST be stop-protected, and a
missing stop is a bug or a data error that must never silently read as zero
risk — but it is WRONG for the core, whose market exposure is captured by the
weight rules (L1/L2/L3/L4/L5/L11), not by a stop.

ADR-0014 redefines L7 as stop-based and CORE-AWARE: a core position contributes
ZERO open risk; a NON-core stopless position still fails closed to its full
value. The engine therefore needs to DISTINGUISH the two, and the distinction
must be POSITIVE (an explicit core marker) — never inferred from the mere
absence of a stop, which would silently zero a satellite's dropped-stop risk.

This column IS that positive marker. It defaults false, so every existing row
and every agent settlement (which does not mention it) stays a satellite bound
by the fail-closed rule; only a settlement of an origin='core_allocation'
proposal sets it true (atlas/dcp/trading/proposals.py::_record_fill). The engine
change that consumes it is the reviewed, 100%-branch-covered task gated behind
ADR-0014; this migration is only the schema half.

DOWNGRADE drops the column. is_core carries no data another table depends on
(the L7 relaxation simply reverts to fail-closing every stopless position, the
pre-ADR-0014 behavior), so the drop is unconditional.

Revision ID: 0023
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Positive core marker. DEFAULT false => existing rows and every agent
    -- settlement remain satellites bound by the L7 fail-closed rule; only a
    -- core_allocation settlement flips it true (invariant-preserving default).
    ALTER TABLE trading.positions
      ADD COLUMN is_core boolean NOT NULL DEFAULT false;
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE trading.positions DROP COLUMN is_core;
    """)
