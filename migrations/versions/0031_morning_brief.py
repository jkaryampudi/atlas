"""reporting.morning_brief — one persisted, deterministic Principal brief per
session (ops-reliability build, 2026-07).

WHY THIS TABLE EXISTS (observed operational failures, not hypotheticals):
core proposals silently expired unapproved twice; four API-billing outages in
five days left the desk producing nothing while nothing said so; the
Principal had no single morning surface where "what ran, what failed, what
needs my click, and how long before it dies" is one read. The brief is
ASSEMBLED, never computed: after t9 the cycle's t9b node collects EXISTING
rows (workflow node results, memos, the approval queue with expiry
countdowns, stored attribution, band/CUSUM status, the learning one-liner,
budget spend, urgent-alert events) into one jsonb document.

One row per session_date (UNIQUE — the idempotent upsert target): a same-day
re-assembly replaces the payload in place, never duplicates. created_at is
first-assembly time, updated_at moves with each upsert; both carry the
injected clock (CLAUDE.md invariant 6), never DB now(). The payload is the
API/console contract (GET /v1/reporting/brief/latest) and is deliberately
self-contained: the console renders the morning view from this row alone,
even when every other endpoint is down.

Revision ID: 0031
"""
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE reporting.morning_brief (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      session_date date NOT NULL UNIQUE,   -- one brief per session; upsert target
      payload jsonb NOT NULL,              -- the assembled brief document
      created_at timestamptz NOT NULL,     -- injected clock, never DB now()
      updated_at timestamptz NOT NULL      -- moves on each idempotent re-assembly
    );

    GRANT USAGE ON SCHEMA reporting TO atlas_agent_reader;
    GRANT SELECT ON reporting.morning_brief TO atlas_agent_reader;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reporting.morning_brief;")
