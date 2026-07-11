"""Shadow runs for model upgrades (ADR-0005 pattern 4; Constitution 7.2):
a shadow run is logged like any run but marked non-actionable.

Revision ID: 0008
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE research.agent_runs "
               "ADD COLUMN shadow boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE research.agent_runs DROP COLUMN IF EXISTS shadow")
