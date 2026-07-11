"""Daily FX ingestion (Doc 05 `market.fx_rates_daily`).

Writes base->AUD rates for every non-AUD instrument currency, so the portfolio
snapshot (`fx_to_aud`) and risk rule L11 always have same-day translation rates.
Usage: python -m atlas.dcp.market_data.fx --date 2026-07-10
"""
from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.dcp.market_data.adapters.base import MarketDataAdapter

BASE_CURRENCY = "AUD"


def required_pairs(session: Session) -> list[tuple[str, str]]:
    """(base, quote) pairs to ingest: every active instrument currency -> AUD."""
    ccys = session.execute(text(
        "SELECT DISTINCT currency FROM market.instruments WHERE is_active")).scalars()
    return sorted((c, BASE_CURRENCY) for c in ccys if c != BASE_CURRENCY)


def upsert_rate(session: Session, *, base: str, quote: str, day: date,
                rate: Decimal, source: str) -> None:
    session.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES (:b, :q, :d, :r, :src) "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate=:r, source=:src"),
        {"b": base, "q": quote, "d": day, "r": rate, "src": source})


def ingest_fx(*, session: Session, adapter: MarketDataAdapter, audit: PostgresAuditLog,
              day: date, pairs: list[tuple[str, str]] | None = None) -> int:
    """Fetch and upsert the day's rates. Returns how many were written; pairs the
    vendor has no rate for (weekend/holiday) are reported in the audit payload."""
    pairs = required_pairs(session) if pairs is None else pairs
    written, missing = 0, []
    for base, quote in pairs:
        rate = adapter.fetch_fx(base, quote, day)
        if rate is None:
            missing.append(f"{base}{quote}")
            continue
        upsert_rate(session, base=base, quote=quote, day=day, rate=rate,
                    source=type(adapter).__name__)
        written += 1
    audit.append(event_type="market.fx.ingested", entity_type="market", entity_id="fx",
                 actor_type="scheduler", actor_id="ingest_fx",
                 payload={"day": day.isoformat(), "written": written, "missing": missing})
    return written


def main() -> None:
    from pathlib import Path

    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters import adapter_from_settings

    p = argparse.ArgumentParser(description="Ingest one day of FX rates")
    p.add_argument("--date", required=True)
    day = date.fromisoformat(p.parse_args().date)
    root = Path(__file__).resolve().parents[3]
    adapter = adapter_from_settings(fixtures_root=root / "tests" / "fixtures",
                                    seeds_csv=root / "seeds" / "instruments_seed.csv")
    clock_dt = datetime(day.year, day.month, day.day, 22, 0, tzinfo=UTC)
    from atlas.core.clock import FrozenClock
    with session_scope() as s:
        audit = PostgresAuditLog(s, FrozenClock(clock_dt))
        written = ingest_fx(session=s, adapter=adapter, audit=audit, day=day)
    print(f"fx {day}: wrote {written} rate(s)")


if __name__ == "__main__":
    main()
