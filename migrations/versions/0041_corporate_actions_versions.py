"""Bitemporal knowledge-time versioning for corporate actions (F-007, part 2).

Bars are stored RAW and split/dividend adjustment is applied ON READ, so
market.corporate_actions is the DOMINANT mutable input to every adjusted / total
-return number. Today record_split/record_dividend use ON CONFLICT DO NOTHING
(first-write-wins) and the table has NO temporal column: a corrected ratio/amount
is silently dropped, and a NEW action added later retroactively changes what a
past run's adjustment produced — so an adjusted number is not reproducible
'as the data stood at run time' even with bars versioned.

This adds the same knowledge-time axis as bars (migration 0040):

  * market.corporate_actions_versions — an APPEND-ONLY history keyed by
    (instrument_id, action_date, action_type, knowledge_date), immutable via a
    trigger (UPDATE/DELETE refused).
  * The head market.corporate_actions is LEFT UNCHANGED (record_* still writes it
    exactly as before), so every existing corporate-action reader is byte-
    identical. Version capture is done explicitly in record_split/record_dividend
    (a DO-NOTHING head never surfaces a correction to a trigger), appending a new
    version only when the incoming value differs from the latest known one.
  * A genesis baseline seeds the current ~29k actions at the initial-backfill
    epoch (min bar ingested_at) — a knowledge FLOOR so every run's as-of read
    (run date >> backfill) sees the baseline. Corporate actions carry no
    first-record timestamp, so this floor is the honest anchor; it does NOT claim
    to know when each action truly landed, and does not recover any correction
    silently dropped before versioning.

As-of reads (load_splits_asof / load_dividends_asof) cap on BOTH action_date <= t
(event/look-ahead, unchanged) AND knowledge_date <= K (reproducibility); with
K=now they equal the head, so validated numbers are unchanged until a run pins K.

Revision ID: 0041
"""
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS market.corporate_actions_versions (
        id             bigserial PRIMARY KEY,
        instrument_id  uuid NOT NULL,
        action_date    date NOT NULL,
        action_type    text NOT NULL,
        ratio          numeric,
        amount         numeric,
        currency       char(3),
        source         text NOT NULL,
        knowledge_date timestamptz NOT NULL,
        is_genesis     boolean NOT NULL DEFAULT false
    );

    CREATE INDEX IF NOT EXISTS ix_cav_asof
        ON market.corporate_actions_versions
           (instrument_id, action_type, action_date, knowledge_date DESC, id DESC);

    -- Append-only immutability (same contract as price_bars_versions).
    CREATE OR REPLACE FUNCTION market.guard_corp_action_version() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'market.corporate_actions_versions is append-only (F-007) — % refused',
            TG_OP;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS cav_immutable ON market.corporate_actions_versions;
    CREATE TRIGGER cav_immutable
        BEFORE UPDATE OR DELETE ON market.corporate_actions_versions
        FOR EACH ROW EXECUTE FUNCTION market.guard_corp_action_version();
    """)

    # Genesis baseline: the current head is the first-known version. Corporate
    # actions have no first-record timestamp, so stamp them at the initial-backfill
    # epoch (the earliest bar ingested_at) — a knowledge FLOOR that keeps the
    # baseline visible to every real run (run date >> backfill).
    op.execute("""
    INSERT INTO market.corporate_actions_versions
        (instrument_id, action_date, action_type, ratio, amount, currency, source,
         knowledge_date, is_genesis)
    SELECT ca.instrument_id, ca.action_date, ca.action_type, ca.ratio, ca.amount,
           ca.currency, ca.source,
           COALESCE((SELECT min(ingested_at) FROM market.price_bars_daily), now()),
           true
    FROM market.corporate_actions ca;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TRIGGER IF EXISTS cav_immutable ON market.corporate_actions_versions;
    DROP FUNCTION IF EXISTS market.guard_corp_action_version();
    DROP TABLE IF EXISTS market.corporate_actions_versions;
    """)
