"""Resumable workflow checkpoints (ADR-0005 pattern 3): each daily-cycle node
persists its result before the next runs; a rerun of the same run_id skips
completed nodes.

Revision ID: 0007
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE SCHEMA IF NOT EXISTS workflow;

    CREATE TABLE workflow.workflow_runs (
      run_id text PRIMARY KEY,
      started_at timestamptz NOT NULL,
      status text NOT NULL CHECK (status IN ('running','completed','failed')),
      completed_at timestamptz
    );

    CREATE TABLE workflow.workflow_node_results (
      run_id text NOT NULL REFERENCES workflow.workflow_runs(run_id),
      node_name text NOT NULL,
      status text NOT NULL CHECK (status IN ('done','failed')),
      output_ref text,
      completed_at timestamptz NOT NULL,
      PRIMARY KEY (run_id, node_name)
    );
    """)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS workflow CASCADE")
