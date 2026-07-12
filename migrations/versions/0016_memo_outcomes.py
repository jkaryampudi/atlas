"""research.memo_outcomes — the desk graded against its own record.

One row per (memo, horizon): what the recommended instrument actually did over
the `horizon_sessions` sessions after the memo's anchor session, against SPY
over the SAME dates (ADR-0009: buy-and-hold the market is the fund's honest
alternative, so the benchmark is relative, never absolute). Rows are written by
atlas/dcp/scorecard.py only once an outcome has MATURED — the forward bar
exists — and are append-only by convention (same discipline as
research.memo_evidence and market.fundamentals): an outcome, once matured and
recorded, is a historical fact; it is never UPDATEd or DELETEd, and re-runs are
idempotent via UNIQUE (memo_id, horizon_sessions).

Returns are stored at the 6dp quantum of the return columns:
fwd_return = fwd_close/anchor_close - 1, spy_return likewise over the same
anchor->fwd dates, excess = fwd_return - spy_return (exact at 6dp because both
operands are already quantized).

Revision ID: 0016
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE research.memo_outcomes (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      memo_id uuid NOT NULL REFERENCES research.memos(id),
      horizon_sessions int NOT NULL CHECK (horizon_sessions IN (20, 60)),
      anchor_date date NOT NULL,                  -- session the memo's evidence ended on
      anchor_close numeric(18,6) NOT NULL,
      fwd_close numeric(18,6) NOT NULL,
      fwd_return numeric(12,6) NOT NULL,
      spy_return numeric(12,6) NOT NULL,          -- SPY over the SAME anchor->fwd dates
      excess numeric(12,6) NOT NULL,              -- fwd_return - spy_return
      computed_at timestamptz NOT NULL,
      UNIQUE (memo_id, horizon_sessions)
    );

    GRANT SELECT ON research.memo_outcomes TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research.memo_outcomes;")
