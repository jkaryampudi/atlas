"""market.quarterly_fundamentals — immutable quarterly-statement FACTS for the
quality (GP/A) factor.

One row per (instrument, fiscal_period_end): the quarter's income-statement
gross profit and total revenue plus the balance-sheet total assets, merged from
the vendor's Financials::Income_Statement::quarterly and
::Balance_Sheet::quarterly blocks, and the `filing_date` on which those figures
became PUBLIC. These feed the quality / gross-profitability signal
(atlas/dcp/signals/quality/v1.py, Novy-Marx 2013 GP/A) and its point-in-time
backtest.

`filing_date` IS THE KNOWABILITY ANCHOR (the analogue of earnings_surprises'
report_date): quarterly figures are knowable to a trader only once the filing
lands, never at the fiscal period end. The signal gates every read on it —
effective session = the first session AFTER filing_date (filings land after
the close or intraday; treated conservatively like PEAD's after-market rule).
The CHECK (filing_date > fiscal_period_end) enforces anchorability at the
storage layer: a live-data probe (2026-07, AVGO/AAPL/ATVI) found the vendor
stamps filing_date = fiscal_period_end on a large minority of quarters (46/78
for AVGO, all of 2012-2017) — a physically impossible date (no company files
the day its quarter ends) that would inject WEEKS of look-ahead if trusted.
Such rows are dropped fail-closed at ingestion, counted and reported
(atlas/dcp/market_data/quarterly_fundamentals.py).

WHY THIS IS AN IMMUTABLE FACT STORE. A filed quarterly statement is a settled
historical fact. Ingestion is append-only: ON CONFLICT (instrument_id,
fiscal_period_end) DO NOTHING — a stored row is NEVER updated and re-ingestion
is idempotent (mirrors market.earnings_surprises, 0021). RESTATEMENT CAVEAT,
recorded honestly: the vendor keeps ONE figure per quarter (the latest-filed
value) with no as-originally-reported archive. Live probes match the original
10-Q figures exactly (AAPL 2018-12-31: grossProfit 32,031M / totalRevenue
84,310M / totalAssets 373,719M / filed 2019-01-30 — the as-reported values),
so figures appear as-reported in practice; but a restated quarter fetched
AFTER its restatement would carry the restated value with the ORIGINAL
filing_date. Restatements are rare; this is a known, documented limitation of
the vendor, not silently ignored. Our append-only store freezes whatever the
vendor served at first fetch.

Metric columns are NULLable: the vendor legitimately omits figures (old
balance sheets missing totalAssets, a few income quarters missing grossProfit
— AVGO has 4/78). Missing is missing: the signal treats an absent grossProfit
as ineligible, NEVER derives it from totalRevenue minus a cost line
(fail-closed; the coverage cost is reported). A row is stored when the quarter
is anchorable and carries at least one metric.

`fetched_at` comes from the injected clock (CLAUDE.md invariant 6), never
DB-side now().

Revision ID: 0026
"""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE market.quarterly_fundamentals (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      instrument_id uuid NOT NULL REFERENCES market.instruments(id),
      fiscal_period_end date NOT NULL,   -- vendor quarterly key (period end)
      -- the knowability anchor: when the figures became PUBLIC; strictly after
      -- the period end (vendor rows stamped filing_date = period end are a
      -- probed data defect and are dropped fail-closed at ingestion)
      filing_date date NOT NULL,
      gross_profit numeric NULL,         -- Income_Statement grossProfit; missing is missing
      total_revenue numeric NULL,        -- Income_Statement totalRevenue
      total_assets numeric NULL,         -- Balance_Sheet totalAssets
      currency text NULL,                -- statement currency_symbol
      source text NOT NULL,
      fetched_at timestamptz NOT NULL,   -- injected clock, never DB now()
      CHECK (filing_date > fiscal_period_end),
      -- natural key: append-only ingest upserts DO NOTHING on it, so re-runs
      -- never duplicate and a settled fact is never overwritten
      UNIQUE (instrument_id, fiscal_period_end)
    );
    -- the signal reads "events with filing_date <= T per instrument"
    CREATE INDEX quarterly_fundamentals_filing_idx
      ON market.quarterly_fundamentals (instrument_id, filing_date);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market.quarterly_fundamentals;")
