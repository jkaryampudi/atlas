"""F-007 — knowledge-time (bitemporal) reads of corporate actions.

market.corporate_actions_versions (migration 0041) is an append-only history of
every split/dividend value and when it became known. Bars are stored RAW and
adjustment is read-side, so corporate actions are the dominant mutable input to
adjusted / total-return numbers; versioning them (alongside bars) is what makes a
past run's ADJUSTED output reproducible as-of its run date.

The head market.corporate_actions is written exactly as before (byte-identical
live reads). append_corp_action_version captures a NEW version only when the
incoming value differs from the latest known one, stamping knowledge_date from
the ingest's GUC (atlas.knowledge_date, else now()). As-of reads cap on BOTH
action_date <= through (event/look-ahead) AND knowledge_date <= known_by
(reproducibility); with known_by=now they return the head values.

Pure DCP; knowledge dates come from the injected clock (via the GUC), never
datetime.now().
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.market_data.models import Dividend, Split

# reads knowledge_date from the session GUC set by the ingest (bar_versions.
# set_knowledge_date), falling back to the DB clock — identical convention to the
# bar-version trigger.
_KD = "COALESCE(NULLIF(current_setting('atlas.knowledge_date', true),'')::timestamptz, now())"


def append_corp_action_version(session: Session, instrument_id: object, *,
                               action_date: date, action_type: str,
                               ratio: object = None, amount: object = None,
                               currency: str | None = None, source: str) -> None:
    """Append a knowledge-time version of a corporate action IF it is new or its
    value changed from the latest known version (so an unchanged re-ingest adds
    nothing, a correction adds a dated version). The head table is written
    separately by the caller and is left untouched."""
    session.execute(text(f"""
        INSERT INTO market.corporate_actions_versions
            (instrument_id, action_date, action_type, ratio, amount, currency,
             source, knowledge_date, is_genesis)
        SELECT :iid, :d, :t, :r, :a, :cur, :src, {_KD}, false
        WHERE NOT EXISTS (
            SELECT 1 FROM (
                SELECT ratio, amount, currency FROM market.corporate_actions_versions
                WHERE instrument_id = :iid AND action_date = :d AND action_type = :t
                ORDER BY knowledge_date DESC, id DESC LIMIT 1
            ) latest
            WHERE latest.ratio    IS NOT DISTINCT FROM :r
              AND latest.amount   IS NOT DISTINCT FROM :a
              AND latest.currency IS NOT DISTINCT FROM :cur)
    """), {"iid": instrument_id, "d": action_date, "t": action_type,
           "r": ratio, "a": amount, "cur": currency, "src": source})


def load_splits_asof(session: Session, instrument_id: object, *,
                     known_by: datetime, through: date | None = None,
                     symbol: str = "") -> list[Split]:
    """Split corporate actions for the instrument as they were KNOWN at
    ``known_by`` (latest version per action_date with knowledge_date <= known_by),
    optionally capped to action_date <= ``through`` (event cap). Mirrors the raw
    split loaders; the caller's adjuster applies the event-time rule."""
    rows = session.execute(text("""
        SELECT DISTINCT ON (action_date) action_date, ratio
        FROM market.corporate_actions_versions
        WHERE instrument_id = :iid AND action_type = 'split'
          AND knowledge_date <= :known_by
          AND (CAST(:through AS date) IS NULL OR action_date <= CAST(:through AS date))
        ORDER BY action_date, knowledge_date DESC, id DESC
    """), {"iid": instrument_id, "known_by": known_by, "through": through}).all()
    return [Split(symbol=symbol or str(instrument_id), action_date=r.action_date,
                  ratio=Decimal(r.ratio)) for r in rows if r.ratio is not None]


def load_dividends_asof(session: Session, instrument_id: object, *,
                        known_by: datetime, through: date | None = None,
                        symbol: str = "") -> list[Dividend]:
    """Cash dividends for the instrument as they were KNOWN at ``known_by``
    (latest version per ex-date), optionally capped to action_date <= through."""
    rows = session.execute(text("""
        SELECT DISTINCT ON (action_date) action_date, amount, currency
        FROM market.corporate_actions_versions
        WHERE instrument_id = :iid AND action_type = 'dividend'
          AND knowledge_date <= :known_by
          AND (CAST(:through AS date) IS NULL OR action_date <= CAST(:through AS date))
        ORDER BY action_date, knowledge_date DESC, id DESC
    """), {"iid": instrument_id, "known_by": known_by, "through": through}).all()
    return [Dividend(symbol=symbol or str(instrument_id), ex_date=r.action_date,
                     amount=Decimal(r.amount), currency=r.currency)
            for r in rows if r.amount is not None]


def corp_action_version_count(session: Session, instrument_id: object,
                              action_date: date, action_type: str) -> int:
    """How many stored versions one action has (1 = never revised)."""
    return int(session.execute(text(
        "SELECT count(*) FROM market.corporate_actions_versions "
        "WHERE instrument_id = :iid AND action_date = :d AND action_type = :t"),
        {"iid": instrument_id, "d": action_date, "t": action_type}).scalar() or 0)
