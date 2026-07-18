"""research.source_picks: add 1-week (5-session) and 2-week (10-session)
excess-vs-SPY horizons alongside the existing 20/60.

The Principal wants early visibility on how each pick moves in the first week
or two. These short horizons are for WATCHING and hypothesis-forming, not for
deciding: a 1-2 week move on a high-volatility name is mostly noise (the pop
often reverses), so a filter must still be validated on the horizon that
carries signal. Same write-once semantics as excess_20/60 — a matured outcome
is a fact, never revised.

Revision ID: 0034
"""
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE research.source_picks ADD COLUMN excess_5 numeric(12,6)")
    op.execute("ALTER TABLE research.source_picks ADD COLUMN excess_10 numeric(12,6)")
    # widen the ungraded partial index to the new horizons.
    op.execute("DROP INDEX IF EXISTS research.idx_source_picks_ungraded")
    op.execute("CREATE INDEX idx_source_picks_ungraded "
               "ON research.source_picks (recommendation_date) "
               "WHERE excess_5 IS NULL OR excess_10 IS NULL "
               "   OR excess_20 IS NULL OR excess_60 IS NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS research.idx_source_picks_ungraded")
    op.execute("ALTER TABLE research.source_picks DROP COLUMN excess_5")
    op.execute("ALTER TABLE research.source_picks DROP COLUMN excess_10")
    op.execute("CREATE INDEX idx_source_picks_ungraded "
               "ON research.source_picks (recommendation_date) "
               "WHERE excess_20 IS NULL OR excess_60 IS NULL")
