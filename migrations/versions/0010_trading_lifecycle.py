"""Trading lifecycle: risk.risk_checks (Doc 05 §4) + the trading schema (Doc 05 §5)
minus trading.portfolio_snapshots, which shipped in 0001 and is NOT recreated.

Integrity rules from Doc 05 §7 are encoded structurally:
- no trade without evidence: trade_proposals.committee_memo_id NOT NULL,
  signal_ids NOT NULL and non-empty (CHECK cardinality > 0). Postgres cannot
  FK an array column and quant.signals does not exist yet, so signal-id
  RESOLUTION stays procedural until the signals table ships — acknowledged gap;
- no pending_approval without a referenced check (Doc 04 §2.1): CHECK
  (state <> 'pending_approval' OR risk_check_id IS NOT NULL);
- no execution without risk approval: orders.approval_id NOT NULL,
  orders.risk_check_id NOT NULL; approvals.approval_time_risk_check_id NOT NULL
  (the fresh re-check at click, Doc 04 §2.2);
- no double fill: UNIQUE(executions.order_id) — v1 paper fills are always
  full fills; partial fills (Phase 7 live) will relax this deliberately;
- one open position per instrument: UNIQUE(positions.instrument_id)
  WHERE closed_at IS NULL;
- no agent overrides risk: deliberately NO grants on risk.risk_checks (or any
  new table) to atlas_agent_reader.

Resolutions against the Doc 05 §5 sketch:
- created_at is added to every table (Doc 05 conventions: 'created_at
  timestamptz everywhere'), even where the §5 sketch omits it.
- trade_proposals.risk_check_id -> risk.risk_checks and
  risk_checks.proposal_id -> trade_proposals are mutually referencing; the
  proposal-side FK is added via ALTER after both tables exist.
- executions carries decision_price + shortfall_bps: Doc 04 §14 (v1.2)
  requires decision, approval, AND fill prices per trade; decision and fill
  live here, the approval-time price is recorded in the approval-time
  risk_checks.price_snapshot (linked via approvals.approval_time_risk_check_id).

Revision ID: 0010
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE trading.trade_proposals (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      instrument_id uuid REFERENCES market.instruments(id),
      market text,
      action text CHECK (action IN ('buy','sell','reduce','exit')),
      committee_memo_id uuid NOT NULL REFERENCES research.memos(id),
      signal_ids uuid[] NOT NULL CHECK (cardinality(signal_ids) > 0),
      entry_price numeric(18,6) NOT NULL,
      stop_loss numeric(18,6) NOT NULL,
      target_price numeric(18,6) NOT NULL,
      position_size int,
      position_value_aud numeric(18,2),
      risk_check_id uuid,                        -- set on PASS; FK added below
      thesis_summary text,
      risks jsonb NOT NULL DEFAULT '[]',
      confidence text,
      quant_score numeric,
      risk_score numeric,
      state text NOT NULL CHECK (state IN ('draft','risk_review','pending_approval',
                                           'approved','rejected','expired','executed','voided')),
      expires_at timestamptz NOT NULL,           -- 24h TTL
      created_at timestamptz NOT NULL DEFAULT now(),
      -- Doc 04 §2.1: a proposal can only await approval with a referenced check
      CONSTRAINT pending_approval_requires_check
        CHECK (state <> 'pending_approval' OR risk_check_id IS NOT NULL)
    );

    CREATE TABLE risk.risk_checks (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      proposal_id uuid REFERENCES trading.trade_proposals(id),
      limit_set_version int REFERENCES risk.limit_sets(version),
      portfolio_snapshot_id uuid REFERENCES trading.portfolio_snapshots(id),
      price_snapshot jsonb NOT NULL DEFAULT '{}',
      results jsonb NOT NULL,                    -- itemised [{rule, value, limit, pass, detail}]
      verdict text NOT NULL CHECK (verdict IN ('PASS','FAIL')),
      check_kind text CHECK (check_kind IN ('proposal','approval_time','order_time')),
      created_at timestamptz NOT NULL DEFAULT now()
    );

    ALTER TABLE trading.trade_proposals
      ADD CONSTRAINT trade_proposals_risk_check_id_fkey
      FOREIGN KEY (risk_check_id) REFERENCES risk.risk_checks(id);

    CREATE TABLE trading.approvals (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      proposal_id uuid REFERENCES trading.trade_proposals(id),
      decision text CHECK (decision IN ('approve','reject')),
      approver text NOT NULL,
      auth_method text,
      ip inet,
      approval_time_risk_check_id uuid NOT NULL REFERENCES risk.risk_checks(id),
      decided_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE trading.orders (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      proposal_id uuid REFERENCES trading.trade_proposals(id),
      approval_id uuid NOT NULL REFERENCES trading.approvals(id),
      risk_check_id uuid NOT NULL REFERENCES risk.risk_checks(id),
      broker text,
      broker_order_id text,
      side text,
      qty int,
      order_type text,
      limit_price numeric(18,6),
      tolerance_band jsonb NOT NULL DEFAULT '{}',
      state text NOT NULL CHECK (state IN ('pending_submit','submitted','partially_filled',
                                           'filled','cancelled','rejected','error')),
      submitted_at timestamptz,
      closed_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE trading.executions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      order_id uuid NOT NULL REFERENCES trading.orders(id),
      fill_qty int,
      fill_price numeric(18,6),
      fees numeric(18,6),
      fx_rate_used numeric(18,8),
      broker_exec_id text,
      decision_price numeric(18,6),              -- Doc 04 §14: implementation shortfall
      shortfall_bps numeric(12,4),               -- (fill - decision) / decision, in bps
      executed_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE trading.positions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      instrument_id uuid REFERENCES market.instruments(id),
      qty int NOT NULL,
      avg_cost numeric(18,6),
      currency char(3),
      opened_at timestamptz,
      closed_at timestamptz,
      current_stop numeric(18,6),
      thesis_memo_id uuid REFERENCES research.memos(id),
      kill_criteria jsonb NOT NULL DEFAULT '[]',
      created_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE trading.tax_lots (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      position_id uuid REFERENCES trading.positions(id),
      execution_id uuid REFERENCES trading.executions(id),
      qty int,
      cost_aud numeric(18,2),
      acquired_at timestamptz,
      disposed_at timestamptz,
      proceeds_aud numeric(18,2),
      created_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE trading.reconciliations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      as_of date,
      broker text,
      status text CHECK (status IN ('clean','break')),
      diffs jsonb NOT NULL DEFAULT '[]',
      resolved_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now()
    );

    -- schema backstops against double fills / split positions (races cannot
    -- rely on application-level locking alone)
    CREATE UNIQUE INDEX executions_one_full_fill_per_order
      ON trading.executions(order_id);
    CREATE UNIQUE INDEX positions_one_open_per_instrument
      ON trading.positions(instrument_id) WHERE closed_at IS NULL;

    -- hot lifecycle lookups (proposals by state/TTL, orders by state, lineage)
    CREATE INDEX trade_proposals_state_expires_idx
      ON trading.trade_proposals(state, expires_at);
    CREATE INDEX orders_state_idx ON trading.orders(state);
    CREATE INDEX risk_checks_proposal_idx ON risk.risk_checks(proposal_id);
    CREATE INDEX tax_lots_position_idx ON trading.tax_lots(position_id);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE trading.trade_proposals DROP CONSTRAINT trade_proposals_risk_check_id_fkey;
    DROP TABLE IF EXISTS trading.reconciliations, trading.tax_lots, trading.executions,
                         trading.orders, trading.approvals, trading.positions;
    DROP TABLE IF EXISTS risk.risk_checks;
    DROP TABLE IF EXISTS trading.trade_proposals;
    """)
