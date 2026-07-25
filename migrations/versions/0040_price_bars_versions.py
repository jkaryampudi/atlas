"""Bitemporal knowledge-time versioning for daily bars (F-007).

market.price_bars_daily is written by upsert_bar with ON CONFLICT DO UPDATE — an
in-place OVERWRITE that destroys the OHLCV a prior backtest saw (ingested_at is
not even refreshed), so past runs are not reproducible 'as the data stood at run
time'. This adds a knowledge-time axis WITHOUT churning the ~20 existing readers:

  * market.price_bars_versions — an APPEND-ONLY history: one row per (instrument,
    bar_date, knowledge_date). The head table stays the fast 'latest' cache.
  * A trigger on price_bars_daily appends a version on INSERT or a
    content-changing UPDATE (no-op re-ingests append nothing), stamping
    knowledge_date from a session GUC atlas.knowledge_date (set by the ingest from
    the injected clock; falls back to now() if unset) — so versioning is complete
    and cannot be bypassed by any of the 14 write call sites.
  * An immutability trigger refuses UPDATE/DELETE of a version row (the register's
    PG-immutability bar / probe P7: a 100->250 overwrite fails closed). TRUNCATE
    (test cleanup) bypasses row triggers, so it is unaffected.
  * A genesis baseline seeds the current 2.46M bars stamped at their ingested_at
    (their true first-store time). This is truthful for FORWARD reproducibility;
    it does NOT recover the value of any bar overwritten BEFORE versioning — that
    is gone (DO UPDATE kept no copy) and is not fabricated here.

As-of reads (atlas/dcp/market_data/bar_versions.py) return the latest version of
each bar with knowledge_date <= K; with K=now they equal the head, so every
existing reader and validated number is byte-identical. From this cutover
forward, a correction lands as a NEW dated version and a run pinned at K
reproduces exactly.

Revision ID: 0040
"""
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS market.price_bars_versions (
        id             bigserial PRIMARY KEY,
        instrument_id  uuid NOT NULL,
        bar_date       date NOT NULL,
        knowledge_date timestamptz NOT NULL,   -- when this OHLCV became known
        open           numeric,
        high           numeric,
        low            numeric,
        close          numeric,
        volume         bigint,
        source         text NOT NULL,
        is_genesis     boolean NOT NULL DEFAULT false  -- seeded pre-versioning baseline
    );

    -- as-of lookup: latest version of each bar knowable by K
    CREATE INDEX IF NOT EXISTS ix_pbv_asof
        ON market.price_bars_versions (instrument_id, bar_date, knowledge_date DESC, id DESC);

    -- Append-only: a stored version's value can never be rewritten or removed
    -- (F-007 immutability). TRUNCATE does not fire row triggers, so test cleanup
    -- and a downgrade DROP are unaffected.
    CREATE OR REPLACE FUNCTION market.guard_bar_version() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'market.price_bars_versions is append-only (F-007) — % refused',
            TG_OP;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS pbv_immutable ON market.price_bars_versions;
    CREATE TRIGGER pbv_immutable
        BEFORE UPDATE OR DELETE ON market.price_bars_versions
        FOR EACH ROW EXECUTE FUNCTION market.guard_bar_version();

    -- Version-maintenance: append a version on insert or a content-changing
    -- update of the head. knowledge_date from the ingest's GUC, else now().
    CREATE OR REPLACE FUNCTION market.version_bar() RETURNS trigger AS $$
    DECLARE kd timestamptz;
    BEGIN
        IF TG_OP = 'UPDATE'
           AND NEW.open   IS NOT DISTINCT FROM OLD.open
           AND NEW.high   IS NOT DISTINCT FROM OLD.high
           AND NEW.low    IS NOT DISTINCT FROM OLD.low
           AND NEW.close  IS NOT DISTINCT FROM OLD.close
           AND NEW.volume IS NOT DISTINCT FROM OLD.volume
           AND NEW.source IS NOT DISTINCT FROM OLD.source THEN
            RETURN NEW;   -- unchanged re-ingest -> no new version (no bloat)
        END IF;
        kd := COALESCE(
            NULLIF(current_setting('atlas.knowledge_date', true), '')::timestamptz,
            now());
        INSERT INTO market.price_bars_versions
            (instrument_id, bar_date, knowledge_date, open, high, low, close,
             volume, source, is_genesis)
        VALUES (NEW.instrument_id, NEW.bar_date, kd, NEW.open, NEW.high, NEW.low,
                NEW.close, NEW.volume, NEW.source, false);
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS pbv_maintain ON market.price_bars_daily;
    CREATE TRIGGER pbv_maintain
        AFTER INSERT OR UPDATE ON market.price_bars_daily
        FOR EACH ROW EXECUTE FUNCTION market.version_bar();
    """)

    # Genesis baseline: the current head IS the first-known version, stamped at
    # its true first-store time (ingested_at). Set-based; the maintenance trigger
    # is on the HEAD table, so this INSERT into the versions table does not fire
    # it, and the immutability trigger permits INSERT.
    op.execute("""
    INSERT INTO market.price_bars_versions
        (instrument_id, bar_date, knowledge_date, open, high, low, close, volume,
         source, is_genesis)
    SELECT instrument_id, bar_date, COALESCE(ingested_at, now()),
           open, high, low, close, volume, source, true
    FROM market.price_bars_daily;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TRIGGER IF EXISTS pbv_maintain ON market.price_bars_daily;
    DROP TRIGGER IF EXISTS pbv_immutable ON market.price_bars_versions;
    DROP FUNCTION IF EXISTS market.version_bar();
    DROP FUNCTION IF EXISTS market.guard_bar_version();
    DROP TABLE IF EXISTS market.price_bars_versions;
    """)
