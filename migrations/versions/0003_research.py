"""Research schema: agent runs + memos (Doc 05 §2).

Revision ID: 0003
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE research.agent_runs (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      agent_role text NOT NULL,
      prompt_template_hash text NOT NULL,
      model text NOT NULL, model_version text NOT NULL DEFAULT '',
      input_refs jsonb NOT NULL DEFAULT '[]',
      output_ref text, output_hash text,
      status text CHECK (status IN ('ok','schema_fail','timeout','budget_kill')),
      tokens_in int DEFAULT 0, tokens_out int DEFAULT 0,
      cost_usd numeric(10,4) DEFAULT 0, latency_ms int DEFAULT 0,
      workflow_run_id uuid, created_at timestamptz DEFAULT now()
    );

    CREATE TABLE research.memos (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      agent_run_id uuid REFERENCES research.agent_runs(id),
      memo_type text CHECK (memo_type IN ('research','macro','sector','committee',
        'pm_review','attribution','risk_narrative','validation','scanner')),
      instrument_symbol text, sleeve text,
      recommendation text CHECK (recommendation IN
        ('BUY','HOLD','REJECT','EXIT','REDUCE','ADD','WATCHLIST',
         'INSUFFICIENT_EVIDENCE','N/A')),
      conviction text CHECK (conviction IN ('LOW','MEDIUM','HIGH','N/A')),
      thesis text, kill_criteria jsonb DEFAULT '[]',
      evidence_refs jsonb NOT NULL DEFAULT '[]',
      dissent text, created_at timestamptz DEFAULT now()
    );

    GRANT USAGE ON SCHEMA research TO atlas_agent_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA research TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research.memos, research.agent_runs;")
