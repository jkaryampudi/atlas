"""validation schema: point-in-time index membership (the definitive xsmom test).

One table: the vendor's HistoricalTickerComponents snapshot for an index
(GSPC.INDX first), persisted verbatim — nullable start/end dates INCLUDED,
because the gaps themselves are the data-quality facts the membership rule
fails closed on (atlas/dcp/market_data/index_membership.py documents the rule).

The `validation` schema is NEW and deliberately SEALED like fxlab (ADR-0008
discipline): no grants of any kind to atlas_agent_reader. Membership
reconstruction is validation-plane machinery — nothing in the reasoning plane
may see it, and nothing here feeds the tradable universe, the scanner, the
desk or gate coverage.

Revision ID: 0015
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE SCHEMA IF NOT EXISTS validation;

    CREATE TABLE validation.index_membership (
      index_code text NOT NULL,              -- vendor index code, e.g. 'GSPC.INDX'
      ticker text NOT NULL,                  -- vendor ticker code, e.g. 'ABMD'
      name text,                             -- vendor company name (reporting aid)
      start_date date,                       -- NULL = join date unrecorded by vendor
      end_date date,                         -- NULL = still a member (or unrecorded)
      is_active_now boolean NOT NULL,
      is_delisted boolean NOT NULL,
      fetched_at timestamptz NOT NULL,
      PRIMARY KEY (index_code, ticker)
    );

    -- Deliberately NO grants to atlas_agent_reader: the validation plane is
    -- sealed (same discipline as fxlab in 0014 and risk.* in 0001).
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS validation.index_membership;
    DROP SCHEMA IF EXISTS validation;
    """)
