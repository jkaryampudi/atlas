"""research.memo_specialists — the specialist panel's validated assessments
persisted verbatim with the memo they informed (ADR-0011 step 2).

PROVENANCE, NOT CACHE (same discipline as research.memo_debate, 0019): the
panel is rendered into the CIO context via SpecialistPanel.summary_context()
and then discarded — that render is lossy, and the validated
SpecialistAssessment JSONs are unreconstructible later. One row per
(memo, role) stores each assessment EXACTLY as validated, in the same
transaction as the memo row (atlas/agents/roles/cio.py).

ABSENCE IS NO ROW: specialists are fail-soft per seat (unlike the debate,
which is load-bearing for the memo) — a specialist that cage-failed, died in
transport, or had an empty evidence lane persists nothing here; its failed
runs already live in research.agent_runs and on the audit chain, and the CIO
context stated the absence. Scanner-only names run no panel at all (signal-
lane gating, desk.py). A cage-failed MEMO persists no memo and therefore no
specialist rows. Memos that predate this table have no rows here; readers
must present that honestly, never backfill from re-run specialists.

Roles match the actual panel structure (atlas/agents/roles/specialists.py
SPECIALIST_ROLES): quality, growth, macro. Append-only by convention — a row
is never UPDATEd; what the CIO read is a historical fact.

Revision ID: 0025
"""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE research.memo_specialists (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      memo_id uuid NOT NULL REFERENCES research.memos(id),
      role text NOT NULL CHECK (role IN ('quality', 'growth', 'macro')),
      payload jsonb NOT NULL,           -- the validated SpecialistAssessment, verbatim
      created_at timestamptz NOT NULL DEFAULT now(),
      -- exactly one assessment per specialist seat per memo
      UNIQUE (memo_id, role)
    );

    GRANT SELECT ON research.memo_specialists TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research.memo_specialists;")
