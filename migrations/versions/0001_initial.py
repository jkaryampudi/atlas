"""Initial schema: market, quant, risk, trading, audit (Doc 05).

Revision ID: 0001
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE SCHEMA IF NOT EXISTS market;
    CREATE SCHEMA IF NOT EXISTS research;
    CREATE SCHEMA IF NOT EXISTS quant;
    CREATE SCHEMA IF NOT EXISTS risk;
    CREATE SCHEMA IF NOT EXISTS trading;
    CREATE SCHEMA IF NOT EXISTS audit;

    CREATE TABLE market.instruments (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      symbol text NOT NULL,
      exchange text NOT NULL,
      market text NOT NULL CHECK (market IN ('US','IN','AU')),
      instrument_type text CHECK (instrument_type IN ('stock','etf','adr')),
      name text, sector_gics text, industry text,
      economic_exposure text[] DEFAULT '{}',
      currency char(3) NOT NULL,
      is_active boolean DEFAULT true,
      listed_at date,
      created_at timestamptz DEFAULT now(),
      UNIQUE (symbol, exchange)
    );

    CREATE TABLE market.price_bars_daily (
      instrument_id uuid REFERENCES market.instruments(id),
      bar_date date NOT NULL,
      open numeric(18,6), high numeric(18,6), low numeric(18,6),
      close numeric(18,6), adj_close numeric(18,6),
      volume bigint,
      source text NOT NULL,
      ingested_at timestamptz DEFAULT now(),
      quality_flags text[] DEFAULT '{}',
      PRIMARY KEY (instrument_id, bar_date)
    );

    CREATE TABLE market.corporate_actions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      instrument_id uuid REFERENCES market.instruments(id),
      action_date date NOT NULL,
      action_type text CHECK (action_type IN ('split','dividend','bonus','rights','symbol_change')),
      ratio numeric, amount numeric, currency char(3), source text
    );

    CREATE TABLE market.fx_rates_daily (
      base char(3), quote char(3), rate_date date,
      rate numeric(18,8) NOT NULL, source text,
      PRIMARY KEY (base, quote, rate_date)
    );

    CREATE TABLE market.data_quality_gates (
      market text, gate_date date,
      status text CHECK (status IN ('green','amber','red')),
      reasons jsonb DEFAULT '[]',
      PRIMARY KEY (market, gate_date)
    );

    CREATE TABLE risk.limit_sets (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      version int UNIQUE NOT NULL,
      mode text CHECK (mode IN ('institutional','small_aum')),
      limits jsonb NOT NULL,
      effective_from date NOT NULL,
      created_by text NOT NULL,
      confirmation_a timestamptz,
      confirmation_b timestamptz,
      supersedes int,
      CONSTRAINT dual_confirm_gap CHECK (
        confirmation_b IS NULL OR confirmation_b >= confirmation_a + interval '1 hour')
    );

    CREATE TABLE trading.portfolio_snapshots (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      as_of timestamptz NOT NULL,
      nav_aud numeric(18,2) NOT NULL,
      cash_aud numeric(18,2) NOT NULL,
      holdings jsonb NOT NULL DEFAULT '[]',
      exposures jsonb NOT NULL DEFAULT '{}',
      fx_rates jsonb NOT NULL DEFAULT '{}',
      open_risk_pct numeric(8,4)
    );

    CREATE TABLE audit.decision_events (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      seq bigserial UNIQUE,
      event_type text NOT NULL,
      entity_type text, entity_id text,
      actor_type text CHECK (actor_type IN ('dcp','agent','human','scheduler','broker')),
      actor_id text,
      payload jsonb NOT NULL,
      payload_hash text NOT NULL,
      prev_hash text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );

    -- Role scaffolding: audit role is INSERT-only (Doc 05 par.6). Roles are created
    -- idempotently; grants applied when they exist in the target environment.
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'atlas_audit_writer') THEN
        CREATE ROLE atlas_audit_writer NOLOGIN;
      END IF;
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'atlas_agent_reader') THEN
        CREATE ROLE atlas_agent_reader NOLOGIN;
      END IF;
    END $$;
    GRANT USAGE ON SCHEMA audit TO atlas_audit_writer;
    GRANT INSERT ON audit.decision_events TO atlas_audit_writer;
    GRANT USAGE ON SCHEMA market TO atlas_agent_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA market TO atlas_agent_reader;
    -- Deliberately NO grants of any kind on risk.* to atlas_agent_reader (Constitution 3.2).
    """)


def downgrade() -> None:
    op.execute("""
    DROP SCHEMA IF EXISTS trading CASCADE;
    DROP SCHEMA IF EXISTS audit CASCADE;
    DROP SCHEMA IF EXISTS risk CASCADE;
    DROP SCHEMA IF EXISTS quant CASCADE;
    DROP SCHEMA IF EXISTS research CASCADE;
    DROP SCHEMA IF EXISTS market CASCADE;
    """)
