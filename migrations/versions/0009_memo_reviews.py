"""Principal memo reviews (Doc 08 Phase-2 gate: 'human reviews 10 memos') —
makes the sign-off evidenceable instead of implicit.

Revision ID: 0009
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE research.memo_reviews (
      memo_id uuid PRIMARY KEY REFERENCES research.memos(id),
      verdict text NOT NULL CHECK (verdict IN ('agree','disagree')),
      notes text NOT NULL DEFAULT '',
      reviewed_at timestamptz NOT NULL DEFAULT now()
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research.memo_reviews")
