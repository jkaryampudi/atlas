"""F-007 — knowledge-time (bitemporal) reads of daily bars.

market.price_bars_versions (migration 0040) is an append-only history of every
bar value and when it became known. This module reads it 'as of' a knowledge
instant K, so a registered backtest can pin K and reproduce byte-identically even
after a later re-ingest corrects a bar. With K = now the as-of read returns the
same values as the head table market.price_bars_daily, so every existing reader
and validated number is unchanged until it explicitly opts into a pinned K.

Knowledge-time (which VERSION of a bar) is orthogonal to event-time (which
sessions, bar_date <= t) and to corporate-action adjustment (splits/dividends
with action_date <= t) — the three compose by layering, because adjustment is a
pure read-side transform over whatever panel is fetched.

Pure DCP; no agent imports. Knowledge dates come from the injected clock via a
transaction-local GUC (set_knowledge_date), never datetime.now().
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.market_data.models import Bar

KNOWLEDGE_DATE_GUC = "atlas.knowledge_date"


def set_knowledge_date(session: Session, clock: Clock) -> None:
    """Stamp this ingest transaction's knowledge instant from the injected clock,
    so the version-maintenance trigger records a deterministic, testable
    knowledge_date (FrozenClock in tests/replay) instead of the DB wall clock.
    Transaction-local (set_config(..., is_local=true))."""
    session.execute(text("SELECT set_config(:k, :v, true)"),
                    {"k": KNOWLEDGE_DATE_GUC, "v": clock.now().isoformat()})


def load_bars_asof(session: Session, instrument_id: object, *,
                   known_by: datetime, through: date | None = None,
                   source: str | None = None, symbol: str = "") -> list[Bar]:
    """The bars as they were KNOWN at ``known_by`` — the latest version of each
    bar whose knowledge_date <= known_by — optionally capped to sessions
    bar_date <= ``through`` (None = all sessions) and to a single ``source``
    (None = any; pass 'EodhdAdapter' to match the head reader's provenance filter
    exactly, so the as-of read is byte-identical to the head at known_by=now).
    No-look-ahead is structural in both axes: a bar dated after ``through`` or a
    version learned after ``known_by`` is invisible. Ordered by bar_date asc."""
    rows = session.execute(text("""
        SELECT DISTINCT ON (bar_date)
               bar_date, open, high, low, close, volume
        FROM market.price_bars_versions
        WHERE instrument_id = :iid
          AND (CAST(:through AS date) IS NULL OR bar_date <= CAST(:through AS date))
          AND (CAST(:source AS text) IS NULL OR source = CAST(:source AS text))
          AND knowledge_date <= :known_by
        ORDER BY bar_date, knowledge_date DESC, id DESC
    """), {"iid": instrument_id, "through": through, "source": source,
           "known_by": known_by}).all()
    return [Bar(symbol=symbol, bar_date=r.bar_date, open=r.open, high=r.high,
                low=r.low, close=r.close, volume=r.volume) for r in rows]


def bar_version_count(session: Session, instrument_id: object,
                      bar_date: date) -> int:
    """How many stored versions a single bar has (1 = never revised)."""
    return int(session.execute(text(
        "SELECT count(*) FROM market.price_bars_versions "
        "WHERE instrument_id = :iid AND bar_date = :d"),
        {"iid": instrument_id, "d": bar_date}).scalar() or 0)


def revised_bars(session: Session, instrument_id: object) -> list[date]:
    """Bar dates that have been REVISED (>1 stored version) for this instrument —
    the revision-detection the finding asks for."""
    rows = session.execute(text(
        "SELECT bar_date FROM market.price_bars_versions "
        "WHERE instrument_id = :iid "
        "GROUP BY bar_date HAVING count(*) > 1 ORDER BY bar_date"),
        {"iid": instrument_id}).all()
    return [r.bar_date for r in rows]
