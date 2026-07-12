"""EUR/USD daily OHLC ingestion into the sealed fxlab schema (ADR-0008).

The sandbox brings its OWN thin vendor client: the equity adapter protocol is
deliberately NOT extended (the seal cuts both ways — dcp must not know fxlab
exists, and fxlab must not grow tentacles into dcp's adapter registry). Wire
conventions match dcp's EodhdAdapter._get: api_token param, fmt=json,
raise_for_status, transport-injectable for tests.

Two vendor artifacts are DISCARDED at the client, for the same reason:
- volume: EODHD FOREX volume is untrustworthy (frequently 0, verified live) —
  FxBar has no volume field and fxlab.bars_daily no volume column, so nothing
  downstream can lean on it;
- weekend stubs: the vendor emits thin Saturday/Sunday rows at the FX week
  boundary (verified live: ~856 Sundays, ~231 Saturdays over 2010->2026).
  The sandbox's daily frame is Mon-Fri — the frame the weekday-continuity
  check and the candidates' daily-bar citations assume — so weekend rows are
  not sessions and are never stored.

Idempotent upsert (ON CONFLICT DO NOTHING: a stored vendor bar is a recorded
fact; re-ingesting never rewrites history). After every ingest the STORED
series is checked for weekday continuity — FX trades Mon-Fri, so a missing
weekday after the first stored bar is reported (report-only: FOREX holidays
such as Christmas/New Year are real, but the gaps must be visible, never
silent).

Usage: python -m atlas.fxlab.ingest --from 2010-01-01 [--to 2026-07-11]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock, SystemClock
from atlas.fxlab.engine import FxBar

BASE = "https://eodhd.com/api"
PAIR = "EURUSD"
VENDOR_CODE = "EURUSD.FOREX"


class FxlabEodhdClient:
    """fxlab-owned EODHD transport for the one pair the sandbox is allowed."""

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=30)

    def fetch_eurusd(self, start: date, end: date) -> list[FxBar]:
        r = self._client.get(f"{BASE}/eod/{VENDOR_CODE}",
                             params={"api_token": self._key, "fmt": "json",
                                     "from": start.isoformat(), "to": end.isoformat()})
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else []
        bars = [FxBar(bar_date=date.fromisoformat(str(row["date"])),
                      open=float(str(row["open"])), high=float(str(row["high"])),
                      low=float(str(row["low"])), close=float(str(row["close"])))
                for row in rows]
        # Mon-Fri only: vendor weekend stubs are artifacts, not sessions
        return sorted((b for b in bars if b.bar_date.weekday() < 5),
                      key=lambda b: b.bar_date)


def upsert_bars(session: Session, bars: list[FxBar], *, source: str) -> int:
    """Insert vendor bars; existing (pair, bar_date) rows are LEFT ALONE."""
    inserted = 0
    for b in bars:
        res = session.execute(text(
            "INSERT INTO fxlab.bars_daily (pair, bar_date, open, high, low, close, source) "
            "VALUES (:p, :d, :o, :h, :l, :c, :src) "
            "ON CONFLICT (pair, bar_date) DO NOTHING RETURNING 1"),
            {"p": PAIR, "d": b.bar_date, "o": b.open, "h": b.high,
             "l": b.low, "c": b.close, "src": source})
        if res.scalar() is not None:
            inserted += 1
    return inserted


def missing_weekdays(dates: list[date]) -> list[date]:
    """Weekdays absent from the stored series AFTER the first stored bar.
    Weekends are never reported; gaps before the first bar are unknowable
    (the vendor's history simply starts there) and not reported."""
    if len(dates) < 2:
        return []
    have = set(dates)
    out: list[date] = []
    d = dates[0] + timedelta(days=1)
    while d <= dates[-1]:
        if d.weekday() < 5 and d not in have:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass(frozen=True)
class IngestResult:
    fetched: int
    inserted: int
    stored_total: int
    first: date | None
    last: date | None
    weekday_gaps: list[date]


def ingest_eurusd(session: Session, client: FxlabEodhdClient, *,
                  start: date, end: date) -> IngestResult:
    bars = client.fetch_eurusd(start, end)
    inserted = upsert_bars(session, bars, source=type(client).__name__)
    stored = [r.bar_date for r in session.execute(text(
        "SELECT bar_date FROM fxlab.bars_daily WHERE pair = :p ORDER BY bar_date"),
        {"p": PAIR})]
    return IngestResult(fetched=len(bars), inserted=inserted, stored_total=len(stored),
                        first=stored[0] if stored else None,
                        last=stored[-1] if stored else None,
                        weekday_gaps=missing_weekdays(stored))


def main() -> None:
    from atlas.core.config import get_settings
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(description="Ingest EUR/USD daily OHLC into fxlab")
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="end", default=None)
    a = p.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else SystemClock().now().date()

    key = get_settings().eodhd_api_key
    if not key:
        raise SystemExit("ATLAS_EODHD_API_KEY is required — the sandbox has no fixture mode")

    with session_scope() as s:
        res = ingest_eurusd(s, FxlabEodhdClient(key), start=start, end=end)
        if res.last is not None:
            # deterministic clock derived from the data, not the wall (house pattern)
            clock = FrozenClock(datetime(res.last.year, res.last.month, res.last.day,
                                         22, 0, tzinfo=UTC))
            PostgresAuditLog(s, clock).append(
                event_type="fxlab.ingest.completed", entity_type="market",
                entity_id=f"fxlab/{PAIR}", actor_type="dcp", actor_id="fxlab.ingest",
                payload={"pair": PAIR, "window": f"{start}..{end}",
                         "fetched": res.fetched, "inserted": res.inserted,
                         "stored_total": res.stored_total,
                         "weekday_gaps": len(res.weekday_gaps)})

    print(f"fxlab {PAIR} {start}..{end}: fetched {res.fetched}, "
          f"inserted {res.inserted} new, stored total {res.stored_total} "
          f"({res.first}..{res.last})")
    if res.weekday_gaps:
        head = ", ".join(d.isoformat() for d in res.weekday_gaps[:8])
        print(f"weekday-continuity: {len(res.weekday_gaps)} missing weekday(s) after "
              f"first stored bar (FOREX holidays expected; first: {head})")
    else:
        print("weekday-continuity: no missing weekdays")


if __name__ == "__main__":
    main()
