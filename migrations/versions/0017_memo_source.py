"""research.memos.source — external-origin tag for on-demand analyses.

NULL = the desk's own work (the nightly scanner->desk cycle or a manual desk
run). A non-null value (e.g. 'investing.com') records where the Principal got
the idea when they typed a ticker into the console's ANALYZE box, so the
scorecard record can later separate external tips from the desk's own picks.

The tag is persistence-only provenance: it is written by
atlas/agents/roles/cio.py alongside the memo row and NEVER enters any prompt
context — which is exactly why an arbitrary console-supplied string is not a
prompt-injection surface (documented at the committee_memo call site).

Revision ID: 0017
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE research.memos ADD COLUMN source text")


def downgrade() -> None:
    op.execute("ALTER TABLE research.memos DROP COLUMN IF EXISTS source")
