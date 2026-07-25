"""Earnings surprise split-basis anchor (F-009).

market.earnings_surprises stores per-share EPS on the vendor's fetch-time split
basis, append-only (ON CONFLICT (instrument_id, fiscal_period_end) DO NOTHING,
earnings_history.py:170). EODHD backward-adjusts its whole Earnings.History to
the basis current AT FETCH TIME, so within one fetch every quarter shares a
common basis. But a re-ingest AFTER a new stock split re-bases only newly
appearing periods (the vendor's new-basis rows) while DO NOTHING leaves the
already-stored old-basis rows untouched — so one instrument's EPS series can
silently mix two share bases, corrupting the cross-quarter SUE/PEAD comparison
(SUE is invariant to a UNIFORM basis but not to a mixed one).

This adds ``split_basis_asof date`` — the date whose split basis a stored EPS row
is expressed in (= the row's fetched_at date; the vendor already baked in every
split with action_date <= that date). It is the anchor the read path uses to
re-base every row to a single common basis at the knowledge date K, look-ahead
safely: eps_on_basis_K = eps_stored / product(ratio for splits with
split_basis_asof < action_date <= K). Rows stay IMMUTABLE (append-only, never
rewritten) — the reconciliation is a pure read-side transform, exactly like the
price/dividend adjusters. On today's single-fetch panel (all rows fetched
2026-07-15, zero later splits) every factor is 1, so this is preventive
hardening that changes NO current number.

Backfill: existing rows get split_basis_asof = fetched_at::date (their true
basis epoch). The column is left NULLABLE: the store path (store_surprises)
always sets it, and the read-side re-basing treats a NULL basis as a safe no-op
(unknown basis -> never guess/corrupt), so a row without a declared basis is left
untouched rather than mis-adjusted. Reversible.

Revision ID: 0038
"""
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE market.earnings_surprises
      ADD COLUMN IF NOT EXISTS split_basis_asof date;

    -- Every existing row's basis IS the day it was fetched (the vendor adjusted
    -- to that snapshot). Backfill from fetched_at.
    UPDATE market.earnings_surprises
       SET split_basis_asof = (fetched_at AT TIME ZONE 'UTC')::date
     WHERE split_basis_asof IS NULL;
    """)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE market.earnings_surprises DROP COLUMN IF EXISTS split_basis_asof;")
