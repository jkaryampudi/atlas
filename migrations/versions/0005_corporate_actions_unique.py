"""Corporate actions natural key: dedup existing rows, add uniqueness so
ON CONFLICT has an arbiter and backfill re-runs cannot duplicate splits
(review finding: bare ON CONFLICT DO NOTHING never fired — duplicate split
rows would compound N× on read-side adjustment).

Revision ID: 0005
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    -- Remove duplicates first (keep one arbitrary row per natural key; real
    -- data carries a single true action per key).
    DELETE FROM market.corporate_actions a
    USING market.corporate_actions b
    WHERE a.instrument_id = b.instrument_id
      AND a.action_date  = b.action_date
      AND a.action_type  = b.action_type
      AND a.ctid > b.ctid;

    ALTER TABLE market.corporate_actions
      ADD CONSTRAINT corporate_actions_natural_key
      UNIQUE (instrument_id, action_date, action_type);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE market.corporate_actions
      DROP CONSTRAINT IF EXISTS corporate_actions_natural_key;
    """)
