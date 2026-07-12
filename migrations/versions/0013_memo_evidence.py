"""research.memo_evidence — the exact evidence text the agents read, persisted
with the memo it produced.

PROVENANCE, NOT CACHE: build_evidence assembles (ref, body) pairs from live DCP
tables, so the bodies a memo was argued from cannot be reconstructed later once
bars/fundamentals move on. One row per (memo, ordinal) preserves the evidence
set VERBATIM and IN ORDER at the moment the memo landed. Append-only by
convention (same discipline as market.fundamentals): a row is never UPDATEd —
the evidence a decision saw is a historical fact.

Memos that predate this table simply have no rows here; readers must present
that honestly ("evidence bodies not recorded before this feature"), never
backfill bodies from today's data.

Revision ID: 0013
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE research.memo_evidence (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      memo_id uuid NOT NULL REFERENCES research.memos(id),
      ordinal int NOT NULL,                       -- 0-based position in the evidence list
      ref text NOT NULL,                          -- e.g. dcp:bars:AVGO:2026-07-10
      body text NOT NULL,                         -- the exact text the agents read
      created_at timestamptz NOT NULL DEFAULT now(),
      -- one evidence set per memo, positions unambiguous
      UNIQUE (memo_id, ordinal)
    );

    GRANT SELECT ON research.memo_evidence TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research.memo_evidence;")
