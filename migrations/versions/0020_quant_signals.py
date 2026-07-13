"""quant.signals + quant.sleeve_daily — the operational wiring for the first
paper-approved strategy (ADR-0010, xsmom-pit-tr).

quant.signals is the DCP's record of WHAT the approved recipe said and WHEN:
one row per (strategy, instrument, signal session). Rows are produced only by
atlas/dcp/signals/xsmom/generate.py (deterministic compute plane, injected
clock, no look-ahead) and are the REAL signal identities the memo->proposal
bridge attaches to trading.trade_proposals.signal_ids — replacing the interim
uuid5-of-evidence-ref convention of ADR-0006 for memos that cite a signal.
direction is CHECK-limited to 'long': the fund is long-only (CLAUDE.md
preamble); a short signal is a schema violation, not a policy debate.
signal_date is the SESSION whose close formed the signal; valid_until is the
next scheduled rebalance session — between them the sleeve holds (ADR-0010
consequence 4). created_at carries the injected clock, never now().

quant.sleeve_daily is the band-check series (ADR-0010 guardrails): one row per
(strategy, US session) with the sleeve's value, running peak, drawdown, the
stored SPY total-return close for that session, and — once 126 sleeve sessions
exist — the trailing-126-session excess vs SPY TR in percentage points. The
daily cycle's band check (atlas/dcp/trading/bands.py) writes it and enforces
the strategy row's tolerance_bands. sleeve_value is NULLABLE by design: a
NULL row records "the sleeve has never held a lot" (no breach is possible on
an empty sleeve); 0 is a real value for a wound-down sleeve.

quant.strategies.state gains 'suspended': ADR-0010 demotion is machine-
executed and LATCHING — the band check may only ever move paper/live ->
suspended; re-promotion is a Principal signature, never code. The original
CHECK (migration 0004, applied — never edited) is dropped and re-created here
with the new value; downgrade restores it verbatim.

Revision ID: 0020
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_STATES_OLD = "'draft','backtested','validated','approved','live','paper','retired'"
_STATES_NEW = _STATES_OLD + ",'suspended'"


def upgrade() -> None:
    op.execute(f"""
    ALTER TABLE quant.strategies DROP CONSTRAINT strategies_state_check;
    ALTER TABLE quant.strategies ADD CONSTRAINT strategies_state_check
      CHECK (state IN ({_STATES_NEW}));

    CREATE TABLE quant.signals (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      strategy_id uuid NOT NULL REFERENCES quant.strategies(id),
      instrument_id uuid NOT NULL REFERENCES market.instruments(id),
      signal_date date NOT NULL,          -- session whose close formed the signal
      direction text NOT NULL CHECK (direction IN ('long')),
      rank int NOT NULL,
      formation_return numeric NOT NULL,  -- close[t-21]/close[t-252] - 1
      valid_until date NOT NULL,          -- next scheduled rebalance session
      created_at timestamptz NOT NULL,    -- injected clock (CLAUDE.md invariant 6)
      UNIQUE (strategy_id, instrument_id, signal_date)
    );
    CREATE INDEX signals_active_idx ON quant.signals (strategy_id, valid_until);

    CREATE TABLE quant.sleeve_daily (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      strategy_id uuid NOT NULL REFERENCES quant.strategies(id),
      session_date date NOT NULL,
      sleeve_value numeric,               -- NULL = sleeve never initiated
      spy_tr_close numeric,               -- SPY total-return close, this session
      peak_value numeric,
      drawdown numeric,                   -- sleeve_value/peak - 1 (<= 0)
      excess_126s_pp numeric,             -- NULL until 126 sleeve sessions exist
      created_at timestamptz NOT NULL,    -- injected clock
      UNIQUE (strategy_id, session_date)
    );

    GRANT SELECT ON quant.signals, quant.sleeve_daily TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute(f"""
    DROP TABLE IF EXISTS quant.sleeve_daily;
    DROP TABLE IF EXISTS quant.signals;
    ALTER TABLE quant.strategies DROP CONSTRAINT strategies_state_check;
    ALTER TABLE quant.strategies ADD CONSTRAINT strategies_state_check
      CHECK (state IN ({_STATES_OLD}));
    """)
