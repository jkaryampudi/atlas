"""research.source_picks — external source-pick tracking (investing.com etc.):
the MEASUREMENT substrate for "does this source's picks out/underperform, and
can a filter be learned to skew toward the outperformers".

An external pick is NOT a committee memo (Constitution invariant 2: a BUY with
no DCP evidence is a schema violation, and fabricating one would breach the
wall). It gets its OWN row with a point-in-time FEATURE SNAPSHOT (the substrate
a future, gauntlet-validated filter would learn from — unrecoverable if not
captured at recommendation time) and its OWN forward-return grading (excess vs
SPY at 20/60 sessions, the scorecard's exact rule reused).

MEASURED, NEVER APPLIED (learning-loop doctrine): picks are tracked and scored
here; nothing reaches sizing/pricing/execution. A filter that skews the book
toward outperformers is a future Principal-signed, gauntlet-gated decision.

Shape:
- `features` jsonb + `feature_version` — the PIT snapshot; widening the feature
  set is a reviewed version bump (feature-store discipline).
- `excess_20`/`excess_60` are WRITE-ONCE (filled exactly once at maturity from
  NULL, never revised — the honest-record principle; grade_picks enforces the
  WHERE ... IS NULL guard, so a matured outcome is a fact, not a moving number).
- UNIQUE(source, ticker, recommendation_date) — idempotent monthly ingest.

Revision ID: 0033
"""
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE research.source_picks ("
        "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
        "  source text NOT NULL,"
        "  ticker text NOT NULL,"
        "  instrument_id uuid REFERENCES market.instruments(id),"
        "  recommendation_date date NOT NULL,"
        "  as_of_session date NOT NULL,"          # last completed session <= rec_date
        "  source_recommendation text NOT NULL DEFAULT 'BUY',"
        "  feature_version text NOT NULL,"
        "  features jsonb NOT NULL,"
        "  excess_20 numeric(12,6),"              # write-once at maturity
        "  excess_60 numeric(12,6),"
        "  graded_at timestamptz,"
        "  created_at timestamptz NOT NULL DEFAULT now(),"
        "  CONSTRAINT source_picks_unique UNIQUE (source, ticker, recommendation_date),"
        "  CONSTRAINT source_picks_rec_ck CHECK (source_recommendation IN ('BUY','SELL','HOLD')),"
        "  CONSTRAINT source_picks_asof_ck CHECK (as_of_session <= recommendation_date)"
        ")")
    op.execute("CREATE INDEX idx_source_picks_source ON research.source_picks (source)")
    op.execute("CREATE INDEX idx_source_picks_ungraded "
               "ON research.source_picks (recommendation_date) "
               "WHERE excess_20 IS NULL OR excess_60 IS NULL")
    # agents may READ the substrate (same grant posture as memo_outcomes 0016);
    # they hold no write path here (writes are DCP-only, like all research reads).
    op.execute("GRANT SELECT ON research.source_picks TO atlas_agent_reader")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research.source_picks")
