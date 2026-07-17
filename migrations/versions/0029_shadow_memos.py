"""research.shadow_memos — challenger-model committee outputs from shadow
model-upgrade comparisons (Constitution 7.2; ADR-0005 pattern 4).

THE NON-ACTIONABLE GUARANTEE, MADE STRUCTURAL. A shadow comparison re-runs the
full committee path (debate + specialists + CIO) against a past memo's
verbatim evidence with every role forced to a challenger model. Its outputs
are EVIDENCE FOR A REVIEWED REGISTRY CHANGE, never memos: they land in THIS
table and never in research.memos, so nothing that reads research.memos — the
console Research page, the eval harness --db mode, the future memo->proposal
bridge — can ever see, score as production, or act on a shadow output. The
separation is a different table with a different shape, not a flag a code
path could forget to check (the per-run shadow flag from 0008 additionally
marks every underlying agent_run).

One row = one challenger re-run of one source memo within one comparison:
- source_memo_id FK: the production memo whose evidence corpus was replayed
  verbatim (research.memo_evidence, 0013) — the incumbent side of the pair.
- challenger_model: the model string every role was forced to.
- comparison_id: groups the rows of one governed comparison run, the unit the
  report and the audit event describe.
- payload: the full shadow output verbatim — validated CommitteeMemo, all
  four DebateCases, the specialist panel (or its honest absence), the shadow
  CIO run id, the pinned question-template hash, and the memo's attributed
  cost. Evidence bodies are deliberately NOT duplicated here: they are joined
  back from research.memo_evidence via source_memo_id, so the shadow bundle's
  corpus is the source memo's corpus BY CONSTRUCTION, not by copy.

Append-only by convention (same discipline as memo_evidence/memo_debate):
what a challenger produced in a comparison is a historical fact, never
UPDATEd. created_at is written by the caller from the injected clock
(CLAUDE.md invariant 6); the DEFAULT is a safety net only.

Revision ID: 0029
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE research.shadow_memos (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      source_memo_id uuid NOT NULL REFERENCES research.memos(id),
      challenger_model text NOT NULL,
      comparison_id text NOT NULL,
      payload jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      -- one challenger re-run per source memo per comparison: a re-run of a
      -- comparison id is a bug, never a silent second row
      UNIQUE (comparison_id, source_memo_id)
    );

    GRANT SELECT ON research.shadow_memos TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research.shadow_memos;")
