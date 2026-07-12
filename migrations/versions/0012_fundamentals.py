"""market.fundamentals — append-only vendor fundamentals snapshots.

One row per (instrument, fetch date): the RAW vendor document stored whole
(jsonb) for audit and reprocessing. Snapshots are append-style — a stored
payload is never UPDATEd; a newer view of the world is a NEW row with a newer
as_of. UNIQUE(instrument_id, as_of) makes the nightly refresh idempotent
(ON CONFLICT DO NOTHING), never an overwrite.

SECURITY NOTE (prompt injection): the payload contains vendor FREE-TEXT
fields (company description, officer names, addresses). Those fields must
NEVER reach agent prompts — the only reader that feeds agents is
atlas/dcp/market_data/fundamentals.py, which extracts through an explicit
whitelist of numeric and closed-vocabulary paths. Any new reader of this
table that renders text for an LLM must go through that whitelist.

Revision ID: 0012
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE market.fundamentals (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      instrument_id uuid NOT NULL REFERENCES market.instruments(id),
      as_of date NOT NULL,                        -- the fetch date
      payload jsonb NOT NULL,                     -- raw vendor document, whole
      source text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      -- append-style snapshots: one per instrument per fetch date, and the
      -- daily refresh's ON CONFLICT DO NOTHING can never turn into an update
      UNIQUE (instrument_id, as_of)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market.fundamentals;")
