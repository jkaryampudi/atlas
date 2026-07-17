"""market.estimate_snapshots — OUR OWN point-in-time archive of the vendor's
CURRENT earnings-estimate consensus (the ADR-0011 "revisions forward paper
trial" prerequisite).

WHY THIS TABLE EXISTS. EODHD's Earnings::Trend block (earningsEstimateAvg,
epsTrendCurrent/7d/30d, epsRevisionsUp/DownLast7/30days, ...) is a CURRENT
snapshot that the vendor OVERWRITES in place — there is no vendor history of
what the consensus looked like last week (established 2026-07-15, when PEAD
was chosen as the backtestable cousin precisely because strict estimate
revisions cannot be backtested honestly from this vendor). The only path to
ever gauntlet-testing a revisions factor is to record the consensus ourselves,
daily, from today forward. One row here = "this is what the vendor said about
(instrument, fiscal period) on snapshot_date". Every day not recorded is lost
forever; every stored row preserves a value the vendor will later overwrite.

APPEND-ONLY FACTS. A snapshot is what the vendor said on that date — it is
never wrong later, so it is NEVER updated. Ingestion is ON CONFLICT
(instrument_id, fiscal_period_end, snapshot_date) DO NOTHING
(atlas/dcp/market_data/estimate_snapshots.py, the only writer): re-runs are
idempotent, and tomorrow's differing value is a NEW row under tomorrow's
snapshot_date, with today's row untouched. Mirrors the immutability discipline
of market.earnings_surprises (0021) and market.fundamentals (0012), not the
upsert-and-delete discipline of the earnings calendar (0018).

Every metric column is nullable: the vendor genuinely omits legs (a probed
AAPL 2026-03-31 period carried epsRevisionsDownLast7days = null beside
populated up-revisions). NULL means "the vendor showed nothing for this leg on
that date" — an honest recorded absence, never a fabricated zero.

snapshot_date is the injected-clock UTC session the snapshot was taken (the
point-in-time key research will group by); fetched_at is the injected clock's
full timestamp (CLAUDE.md invariant 6 — never DB now()).

NO consumer beyond the tiny read API exists yet BY DESIGN: no signal, no
evidence block, no desk integration until enough forward history has accrued
(roughly >= 6 months of daily snapshots) and a factor spec is written.

Revision ID: 0028
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE market.estimate_snapshots (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      instrument_id uuid NOT NULL REFERENCES market.instruments(id),
      fiscal_period_end date NOT NULL,  -- vendor Earnings::Trend key (period end)
      snapshot_date date NOT NULL,      -- injected-clock UTC session of the snapshot
      -- consensus level (vendor earningsEstimate* / revenueEstimateAvg)
      eps_estimate_avg numeric NULL,
      eps_estimate_analysts numeric NULL,
      revenue_estimate_avg numeric NULL,
      -- vendor's own trailing consensus trail (epsTrendCurrent/7daysAgo/30daysAgo)
      eps_trend_current numeric NULL,
      eps_trend_7d numeric NULL,
      eps_trend_30d numeric NULL,
      -- vendor's analyst up/down revision counts (epsRevisions*Last7/30days)
      revisions_up_7d numeric NULL,
      revisions_up_30d numeric NULL,
      revisions_down_7d numeric NULL,
      revisions_down_30d numeric NULL,
      source text NOT NULL,
      fetched_at timestamptz NOT NULL,  -- injected clock, never DB now()
      -- natural key: append-only ingest does ON CONFLICT DO NOTHING on it, so
      -- a re-run never duplicates and a recorded snapshot is never overwritten
      UNIQUE (instrument_id, fiscal_period_end, snapshot_date)
    );
    -- the once-daily guard asks "any row for this session?"; series reads walk
    -- snapshot_date ranges — both want a snapshot_date-leading index
    CREATE INDEX estimate_snapshots_session_idx
      ON market.estimate_snapshots (snapshot_date);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market.estimate_snapshots;")
