"""market.earnings_calendar — vendor earnings dates per instrument.

One row per (instrument, report_date): the scheduled/recorded earnings report
date (desk-review memo 2026-07 item 9 — ~1 in 3 memos straddles a print and
the scanner's attention heuristic is biased toward earnings-adjacent names
without knowing it). Unlike market.fundamentals this is NOT an append-only
snapshot store: a calendar's future entries are vendor forecasts that get
rescheduled, so the refresh upserts by natural key (fetched_at/when_time may
be updated) and may DELETE a future row the vendor no longer reports — a
moved date must never linger as a phantom print. Past rows (report_date on
or before the refresh day) are facts and are never deleted by the refresh.

`when_time` carries the vendor's before/after-market flag ONLY when it
matches a closed vocabulary (BeforeMarket/AfterMarket — enforced at the
adapter boundary); anything else is stored as NULL. Desk evidence rendered
from this table (atlas/dcp/market_data/earnings.py) is ISO dates and session
counts only — no vendor prose, zero prompt-injection surface.

`fetched_at` comes from the injected clock (CLAUDE.md invariant 6), never
DB-side now(); it drives the staleness-based nightly refresh.

Revision ID: 0018
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE market.earnings_calendar (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      instrument_id uuid NOT NULL REFERENCES market.instruments(id),
      report_date date NOT NULL,      -- vendor `report_date`: the announcement day
      when_time text NULL,            -- closed vocab: BeforeMarket / AfterMarket
      fetched_at timestamptz NOT NULL, -- injected clock, drives staleness refresh
      source text NOT NULL,
      -- natural key: the refresh upserts on it, so re-runs never duplicate
      UNIQUE (instrument_id, report_date)
    );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market.earnings_calendar;")
