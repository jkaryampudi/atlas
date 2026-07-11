"""Quant lifecycle tables: strategies, backtests, validation_reports (Doc 05 §3).

Revision ID: 0004
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE quant.strategies (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      family text NOT NULL, name text NOT NULL, version text NOT NULL,
      spec jsonb NOT NULL DEFAULT '{}',
      code_sha text, tolerance_bands jsonb DEFAULT '{}',
      state text NOT NULL DEFAULT 'draft' CHECK (state IN
        ('draft','backtested','validated','approved','live','paper','retired')),
      approved_by text, approved_at timestamptz,
      created_at timestamptz DEFAULT now(),
      UNIQUE (family, name, version)
    );

    CREATE TABLE quant.backtests (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      strategy_id uuid REFERENCES quant.strategies(id),
      date_start date, date_end date, oos_start date,
      cost_model jsonb DEFAULT '{}', data_bias_summary jsonb DEFAULT '{}',
      metrics jsonb DEFAULT '{}', code_sha text,
      created_at timestamptz DEFAULT now()
    );

    CREATE TABLE quant.validation_reports (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      strategy_id uuid REFERENCES quant.strategies(id),
      backtest_id uuid REFERENCES quant.backtests(id),
      checklist jsonb NOT NULL DEFAULT '{}',
      verdict text CHECK (verdict IN ('approve','reject','revise')),
      reasons text, created_at timestamptz DEFAULT now()
    );

    GRANT SELECT ON ALL TABLES IN SCHEMA quant TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.validation_reports, quant.backtests, "
               "quant.strategies;")
