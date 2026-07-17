"""reporting.attribution_daily — the daily core/satellite NAV decomposition
(Doc 04 §14 monthly attribution's daily substrate; ADR-0012 consequence 4).

ADR-0012 split the book into a passive index core (beta) and an active
satellite (alpha) and signed the consequence: "reporting/attribution must
separate core (beta) from satellite (alpha) so the scorecard measures what the
active strategies actually add over simply holding the index." This table is
that separation, one row per (US session, sleeve):

  core   market value of is_core positions (the ADR-0012 passive core)
  xsmom  the ADR-0010 momentum sleeve (tax lot -> execution -> order ->
         proposal.signal_ids && the strategy's quant.signals — the bands.py
         attribution join, stated once there and shared)
  pead   the ADR-0013 PEAD sleeve, same join
  cash   NAV minus the sleeves (the residual: ledger cash plus anything the
         sleeve joins cannot attribute — v1 has nothing unattributable)
  total  the snapshot NAV itself

value_aud is NOT NULL: in a NAV decomposition an empty sleeve genuinely holds
A$0 (deliberately different from quant.sleeve_daily's NULL = "never
initiated", which grades a strategy, not the book). ret_1d and
benchmark_ret_1d are NULL on the first stored session (a return needs two
observations) and whenever their inputs are missing (no benchmark bar, zero
base) — never a fabricated 0. The daily return is flow-adjusted (a buy is
capital moving cash->sleeve, not sleeve performance); the exact convention is
documented in atlas/dcp/reporting/attribution.py, the only writer.

Benchmarks (ADR-0012 consequence 4, ADR-0009 yardstick): satellite sleeves
and total are graded against SPY TOTAL RETURN; core against the signed 55/15
SPY/INDA target weights renormalized to a fully-invested 55:15 blend; cash
against 0. The sleeve CHECK is deliberately closed: a new sleeve (a third
validated strategy) is a signed schema change, never a silent string.

created_at carries the injected clock (CLAUDE.md invariant 6) and is set on
first insert only — the idempotent re-run upsert never touches it.

Revision ID: 0027
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE SCHEMA IF NOT EXISTS reporting;

    CREATE TABLE reporting.attribution_daily (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      session_date date NOT NULL,        -- US session (last completed at snapshot)
      sleeve text NOT NULL
        CHECK (sleeve IN ('core','xsmom','pead','cash','total')),
      value_aud numeric(18,2) NOT NULL,  -- A$0 is a real value for an empty sleeve
      ret_1d numeric,                    -- flow-adjusted daily return; NULL day one
      benchmark_ret_1d numeric,          -- sleeve's yardstick that day; NULL if absent
      created_at timestamptz NOT NULL,   -- injected clock, never DB now()
      -- one row per (session, sleeve): the idempotent upsert target
      UNIQUE (session_date, sleeve)
    );

    GRANT USAGE ON SCHEMA reporting TO atlas_agent_reader;
    GRANT SELECT ON reporting.attribution_daily TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS reporting.attribution_daily;
    DROP SCHEMA IF EXISTS reporting;
    """)
