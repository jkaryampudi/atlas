"""Learning schema + trial registry (ADR-0002/0003).

Revision ID: 0002
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE SCHEMA IF NOT EXISTS learning;

    CREATE TABLE IF NOT EXISTS quant.trial_registry (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      strategy_family text NOT NULL,
      spec_hash text NOT NULL,
      backtest_id uuid,
      metrics jsonb DEFAULT '{}',
      created_at timestamptz DEFAULT now()
    );

    CREATE TABLE learning.outcome_labels (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      position_id uuid, thesis_memo_id uuid,
      kill_criteria_fired boolean,
      exit_reason_predicted text, exit_reason_actual text,
      pnl_signal numeric, pnl_timing numeric, pnl_cost numeric,
      holding_days int, labeled_at timestamptz DEFAULT now()
    );

    CREATE TABLE learning.counterfactuals (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      source_type text CHECK (source_type IN
        ('rejected_proposal','expired_proposal','stopped_position')),
      source_id uuid, tracked_from date, horizon_days int,
      hypothetical_return numeric, benchmark_return numeric,
      status text CHECK (status IN ('tracking','closed')) DEFAULT 'tracking'
    );

    CREATE TABLE learning.agent_calibration (
      agent_role text, period text, regime text,
      n_forecasts int NOT NULL DEFAULT 0,
      brier_score numeric,
      conviction_weight numeric NOT NULL DEFAULT 1.0,
      prev_weight numeric, updated_at timestamptz DEFAULT now(),
      PRIMARY KEY (agent_role, period, regime)
    );

    CREATE TABLE learning.lessons (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      source_type text, source_id uuid,
      lesson text NOT NULL, tags text[] DEFAULT '{}',
      embedding_ref text, created_at timestamptz DEFAULT now()
    );

    CREATE TABLE learning.adjustments (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tier smallint CHECK (tier IN (1,2)),
      target text NOT NULL, before jsonb, after jsonb,
      evidence_refs jsonb DEFAULT '[]',
      reversible_to uuid, created_at timestamptz DEFAULT now()
    );

    GRANT USAGE ON SCHEMA learning TO atlas_agent_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA learning TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("""
    DROP SCHEMA IF EXISTS learning CASCADE;
    DROP TABLE IF EXISTS quant.trial_registry;
    """)
