"""fxlab schema (ADR-0008): the sealed FX research sandbox's data plane.

One table: EUR/USD daily vendor OHLC. Deliberately minimal and deliberately
SEALED — no grants of any kind to atlas_agent_reader (ADR-0008 §3: nothing in
the reasoning plane may see sandbox data; the seal is structural, mirrored by
tests/unit/test_boundaries_fxlab.py on the code side). There is NO volume
column: EODHD FOREX volume is untrustworthy (frequently 0) and must not exist
to be leaned on.

Revision ID: 0014
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE SCHEMA IF NOT EXISTS fxlab;

    CREATE TABLE fxlab.bars_daily (
      pair text NOT NULL,                         -- 'EURUSD' (ADR-0008: the only pair)
      bar_date date NOT NULL,
      open numeric(12,6) NOT NULL,
      high numeric(12,6) NOT NULL,
      low numeric(12,6) NOT NULL,
      close numeric(12,6) NOT NULL,
      source text NOT NULL,                       -- vendor client class name
      created_at timestamptz DEFAULT now(),
      PRIMARY KEY (pair, bar_date)
    );

    -- Deliberately NO grants to atlas_agent_reader: the sandbox is sealed
    -- (ADR-0008 §3), same discipline as risk.* in 0001.
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS fxlab.bars_daily;
    DROP SCHEMA IF EXISTS fxlab;
    """)
