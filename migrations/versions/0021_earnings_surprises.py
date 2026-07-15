"""market.earnings_surprises — immutable historical earnings-surprise FACTS.

One row per (instrument, fiscal_period_end): a COMPLETED quarterly earnings
report — the announcement day (`report_date`), the reported per-share EPS
(`eps_actual`), the pre-report consensus (`eps_estimate`), the vendor's
split-neutral `surprise_pct`, the reporting `currency`, and the closed-vocab
before/after-market timing flag. These feed the PEAD / earnings-surprise
signal (atlas/dcp/signals/pead/v1.py) and its point-in-time backtest.

WHY THIS IS AN IMMUTABLE FACT STORE (and market.earnings_calendar is NOT).
The earnings *calendar* (migration 0018) stores FUTURE forecast dates that get
rescheduled, so its refresh upserts and may DELETE a moved forecast. This
table is the opposite: a report that has already happened is a settled
historical fact — epsActual/epsEstimate for 2023-Q2 do not change once the
quarter is reported. Ingestion is therefore append-only: ON CONFLICT
(instrument_id, fiscal_period_end) DO NOTHING (see
atlas/dcp/market_data/earnings_history.py). A stored row is NEVER updated;
the natural key makes re-ingestion idempotent. This mirrors the append-only
snapshot discipline of market.fundamentals (0012), not the upsert-and-delete
discipline of the calendar.

`report_date` is the look-ahead anchor: it is the ONLY date on which the
surprise became knowable to the market. The signal gates every read on it
(see the signal module's no-look-ahead construction).

`before_after_market` carries the vendor's timing flag ONLY when it matches
the closed vocabulary (BeforeMarket / AfterMarket — the same
models.EARNINGS_WHEN_TIMES vocabulary the calendar's when_time uses); anything
else is stored as NULL. It disambiguates the tradability boundary: an
after-market print on day D is first actionable the NEXT session.

`surprise_pct` is the vendor's split-neutral surprise ratio (percent); it is
carried as a SECONDARY signal variant for the adversarial cross-check. The
PRIMARY signal is SUE, computed in code from eps_actual/eps_estimate on a
split-adjusted basis (surprise_pct, being a ratio, is split-neutral already).

`eps_actual` / `eps_estimate` are per-share and SPLIT-SENSITIVE; they are
stored RAW (as reported at report_date) exactly like price bars and dividends,
and are split-adjusted ON READ by the signal using the house split convention
(atlas/dcp/market_data/adjustment.py, keyed on report_date). This keeps the
stored value the settled fact and the adjustment auditable.

`fetched_at` comes from the injected clock (CLAUDE.md invariant 6), never
DB-side now().

Revision ID: 0021
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE market.earnings_surprises (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      instrument_id uuid NOT NULL REFERENCES market.instruments(id),
      fiscal_period_end date NOT NULL,   -- vendor Earnings::History key (period end)
      report_date date NOT NULL,         -- announcement day; the look-ahead anchor
      eps_actual numeric NOT NULL,       -- reported per-share EPS, RAW (split-adjust on read)
      eps_estimate numeric NOT NULL,     -- pre-report consensus, RAW (split-adjust on read)
      surprise_pct numeric NULL,         -- vendor split-neutral surprise ratio (secondary)
      currency text NULL,                -- reporting currency (General.CurrencyCode)
      -- closed vocab (models.EARNINGS_WHEN_TIMES); NULL = vendor gave no/other flag
      before_after_market text NULL
        CHECK (before_after_market IN ('BeforeMarket', 'AfterMarket')),
      source text NOT NULL,
      fetched_at timestamptz NOT NULL,   -- injected clock, never DB now()
      -- natural key: append-only ingest upserts DO NOTHING on it, so re-runs
      -- never duplicate and a settled fact is never overwritten
      UNIQUE (instrument_id, fiscal_period_end)
    );
    -- the signal reads "most recent report_date <= T per instrument"
    CREATE INDEX earnings_surprises_report_idx
      ON market.earnings_surprises (instrument_id, report_date);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market.earnings_surprises;")
