"""CIO memo gains a debate_summary (ADR-0005 pattern 1).

Revision ID: 0006
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE research.memos ADD COLUMN debate_summary text NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE research.memos DROP COLUMN IF EXISTS debate_summary")
