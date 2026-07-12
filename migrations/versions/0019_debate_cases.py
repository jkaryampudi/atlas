"""research.memo_debate — the four DebateCase JSONs persisted verbatim with the
memo they informed (desk-review 2026-07 item 7).

PROVENANCE, NOT CACHE (same discipline as research.memo_evidence, 0013): the
debate is today rendered into the CIO context via summary_context() and then
discarded — only the CIO's own debate_summary survives, so whether bull and
bear are anchored copies of each other is unmeasurable, on ~80% of desk spend.
One row per (memo, role) stores each validated DebateCase EXACTLY as the CIO
consumed it, in the same transaction as the memo row (atlas/agents/roles/
cio.py). A cage-failed run persists no memo and therefore no debate rows.

This table UNLOCKS diversity measurement; it deliberately does not do any.
Roles match the actual debate structure (atlas/agents/roles/debate.py
DebateResult): one case each, one rebuttal each. Append-only by convention —
a row is never UPDATEd; what the CIO read is a historical fact. Memos that
predate this table have no rows here; readers must present that honestly,
never backfill from re-run debates.

Revision ID: 0019
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE research.memo_debate (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      memo_id uuid NOT NULL REFERENCES research.memos(id),
      role text NOT NULL CHECK (role IN
        ('bull', 'bear', 'bull_rebuttal', 'bear_rebuttal')),
      payload jsonb NOT NULL,                     -- the validated DebateCase, verbatim
      created_at timestamptz NOT NULL DEFAULT now(),
      -- exactly one case per seat per memo
      UNIQUE (memo_id, role)
    );

    GRANT SELECT ON research.memo_debate TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research.memo_debate;")
