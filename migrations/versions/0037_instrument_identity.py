"""Instrument / issuer identity resolution (F-002; unblocks F-001-full).

The reviewed contamination (ADT / VAL / MNK) is a *reused ticker*: the S&P 500
membership era belongs to a prior, different issuer that used the ticker, while
the stored price series belongs to the current issuer and never overlaps that
era. F-001 already excludes those by date-overlap proxy
(``series_overlaps_membership``); F-002 makes the identity *explicit* and
resolvable, and fails closed where identity is unknown.

What is buildable from real data (this migration + atlas/dcp/market_data/
identity.py): a permanent-identifier model keyed on ISIN (with CUSIP secondary),
populated from ``market.fundamentals.payload->'General'`` — ISINs are present for
the active tradeable set and are unique per instrument. A resolver answers
"who is the issuer behind this instrument (as of a date)?", fails closed when no
permanent identifier is present (the delisted / no-fundamentals rows — exactly
where reused-ticker danger lives), and detects issuer drift under a held
position.

What is NOT reconstructable from ingested data, and is honestly deferred: the
*dated identifier change-history* (known_from/known_to per mapping across a
ticker's life). EODHD's current-fundamentals endpoint serves one snapshot — one
``as_of`` per instrument — not a history of past ISIN↔ticker↔venue changes. So
this table is bitemporal-*capable* (``valid_from`` / ``valid_to`` /
``history_complete``) but today holds exactly one OPEN identity row per
instrument with ``history_complete = false`` and a conservative ``valid_from``
(the first stored bar — the span we can actually attest). Filling in dated
history requires a vendor symbol-change / delisting feed (a procurement
decision, documented in ATLAS_OPERATOR_ACTIONS.md); the schema is shaped to
receive it without rework (append closed historical rows; flip
``history_complete``).

Schema only — population is done by the idempotent ``populate_identities`` /
``refresh_identity`` functions so it is testable and re-runnable, and is wired
into the fundamentals ingest. No numbers, no fabricated identifiers.

Revision ID: 0037
"""
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS market.instrument_identity (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        instrument_id     uuid NOT NULL REFERENCES market.instruments(id) ON DELETE CASCADE,
        isin              text,
        cusip             text,
        figi              text,            -- reserved: no FIGI source ingested yet (always NULL for now)
        primary_ticker    text,
        identity_currency char(3),
        country_iso       text,
        -- bitemporal validity of THIS identity. With a single vendor snapshot we
        -- hold one OPEN row per instrument spanning the range we can attest.
        valid_from        date NOT NULL,   -- earliest date we vouch this identity (first stored bar / snapshot floor)
        valid_to          date,            -- NULL = open/current; departures are unknown so it stays open
        known_from        timestamptz NOT NULL,  -- when we learned it (fundamentals as_of / ingest time)
        identity_source   text NOT NULL,   -- provenance, e.g. 'eodhd_fundamentals'
        history_complete  boolean NOT NULL DEFAULT false,  -- honest: one snapshot, no dated change-history yet
        is_resolved       boolean NOT NULL,  -- a permanent identifier (isin OR cusip) is present
        created_at        timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_instrument_identity_valid_order
            CHECK (valid_to IS NULL OR valid_to > valid_from)
    );

    CREATE INDEX IF NOT EXISTS ix_instrument_identity_instrument
        ON market.instrument_identity (instrument_id);
    CREATE INDEX IF NOT EXISTS ix_instrument_identity_isin
        ON market.instrument_identity (isin) WHERE isin IS NOT NULL;

    -- At most one OPEN (current) identity per instrument. Future dated history is
    -- appended as CLOSED rows (valid_to set), which this partial index permits.
    CREATE UNIQUE INDEX IF NOT EXISTS uq_instrument_identity_open
        ON market.instrument_identity (instrument_id) WHERE valid_to IS NULL;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS market.instrument_identity;")
