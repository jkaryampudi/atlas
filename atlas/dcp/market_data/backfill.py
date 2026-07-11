"""History backfill (Phase 1 exit): bars + corporate actions + per-day quality
gates + FX, over exchange-calendar sessions.

Usage: python -m atlas.dcp.market_data.backfill --years 2 --end 2026-07-10
`--end` is explicit so runs are deterministic and replayable (no wall clock).
Vendor: EODHD when ATLAS_EODHD_API_KEY is set; the fixture adapter otherwise.

Bars are fetched as one range per instrument (not per day), stored RAW with
splits recorded in `market.corporate_actions` — adjustment happens on read.
A RED gate is a reportable result; the exit criterion is zero red gates on a
clean day, not zero red gates by construction.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.fx import required_pairs, upsert_rate
from atlas.dcp.market_data.ingest import (
    record_split,
    seed_instruments,
    upsert_bar,
    write_gate,
)
from atlas.dcp.market_data.models import Bar, GateStatus
from atlas.dcp.market_data.quality import evaluate_gate

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class MarketBackfill:
    market: str
    sessions: int
    bars: int
    red: int
    amber: int
    first_red: tuple[str, ...]  # first few red dates, for the report


@dataclass(frozen=True)
class FxPairSummary:
    pair: str
    rows: int
    missing_weekdays: int  # weekdays with no vendor rate (FOREX holidays expected ~2-3/yr)
    empty: bool            # zero rows over the whole window — always a failure


@dataclass(frozen=True)
class BackfillReport:
    markets: dict[str, MarketBackfill]
    fx: dict[str, FxPairSummary]

    @property
    def failed(self) -> bool:
        return (any(m.red for m in self.markets.values())
                or any(f.empty for f in self.fx.values()))


def _weekdays(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _backfill_market(session: Session, adapter: MarketDataAdapter, market: str,
                     days: list[date], end: date) -> MarketBackfill:
    instruments = session.execute(text(
        "SELECT id, symbol FROM market.instruments "
        "WHERE market = :m AND is_active"), {"m": market}).mappings().all()

    source = type(adapter).__name__
    bars_by_day: dict[date, list[Bar]] = defaultdict(list)
    explained_by_day: dict[date, set[str]] = defaultdict(set)
    n_bars = 0
    for inst in instruments:
        for sp in adapter.fetch_splits(inst["symbol"], days[0], end):
            record_split(session, inst["id"], sp, source)
            explained_by_day[sp.action_date].add(inst["symbol"])
        for b in adapter.fetch_bars(inst["symbol"], days[0], end):
            upsert_bar(session, inst["id"], b, source)
            bars_by_day[b.bar_date].append(b)
            n_bars += 1

    expected_symbols = frozenset(inst["symbol"] for inst in instruments)
    red = amber = 0
    first_red: list[str] = []
    for i, d in enumerate(days):
        window = {k: bars_by_day.get(k, []) for k in days[max(0, i - 1):i + 1]}
        gate = evaluate_gate(market=market, as_of=d, expected_days=[d],
                             bars_by_day=window,
                             explained_symbols=frozenset(explained_by_day.get(d, set())),
                             expected_symbols=expected_symbols)
        write_gate(session, gate)
        if gate.status is GateStatus.RED:
            red += 1
            if len(first_red) < 5:
                first_red.append(d.isoformat())
        elif gate.status is GateStatus.AMBER:
            amber += 1

    return MarketBackfill(market=market, sessions=len(days), bars=n_bars,
                          red=red, amber=amber, first_red=tuple(first_red))


def backfill(*, session: Session, adapter: MarketDataAdapter, audit: PostgresAuditLog,
             markets: list[str], start: date, end: date,
             seeds_csv: Path) -> BackfillReport:
    seed_instruments(session, seeds_csv)

    results: dict[str, MarketBackfill] = {}
    for market in markets:
        days = trading_days_between(market, start, end)
        if not days:
            continue
        results[market] = _backfill_market(session, adapter, market, days, end)

    # FX with reconciliation: an empty series is a hard failure; sparse weekdays
    # are surfaced (FOREX holidays make a few expected). Review finding: FX
    # previously had no effect on success reporting at all.
    fx: dict[str, FxPairSummary] = {}
    weekdays = _weekdays(start, end)
    for base, quote in required_pairs(session):
        series = adapter.fetch_fx_series(base, quote, start, end)
        for d, rate in sorted(series.items()):
            upsert_rate(session, base=base, quote=quote, day=d, rate=rate,
                        source=type(adapter).__name__)
        pair = f"{base}{quote}"
        fx[pair] = FxPairSummary(pair=pair, rows=len(series),
                                 missing_weekdays=sum(1 for d in weekdays
                                                      if d not in series),
                                 empty=not series)

    report = BackfillReport(markets=results, fx=fx)
    audit.append(event_type="market.backfill.completed", entity_type="market",
                 entity_id=",".join(markets), actor_type="scheduler", actor_id="backfill",
                 payload={"start": start.isoformat(), "end": end.isoformat(),
                          "fx": {p: {"rows": f.rows, "missing_weekdays": f.missing_weekdays,
                                     "empty": f.empty} for p, f in fx.items()},
                          "markets": {m: {"sessions": r.sessions, "bars": r.bars,
                                          "red": r.red, "amber": r.amber,
                                          "first_red": list(r.first_red)}
                                      for m, r in results.items()}})
    return report


def _seed_markets(seeds_csv: Path) -> list[str]:
    import csv

    with seeds_csv.open() as f:
        return sorted({row["market"] for row in csv.DictReader(f)})


def main() -> None:
    from atlas.core.clock import FrozenClock
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters import adapter_from_settings

    p = argparse.ArgumentParser(description="Backfill price/FX history with quality gates")
    p.add_argument("--years", type=float, default=2.0)
    p.add_argument("--end", required=True,
                   help="last calendar day (ISO), explicit for determinism")
    p.add_argument("--market", action="append", dest="markets",
                   help="restrict to a market (repeatable); default: all in seeds")
    p.add_argument("--seeds", type=Path, default=ROOT / "seeds" / "instruments_seed.csv")
    a = p.parse_args()

    end = date.fromisoformat(a.end)
    start = end - timedelta(days=round(a.years * 365.25))
    markets = a.markets or _seed_markets(a.seeds)
    adapter = adapter_from_settings(fixtures_root=ROOT / "tests" / "fixtures",
                                    seeds_csv=a.seeds)
    clock = FrozenClock(datetime(end.year, end.month, end.day, 22, 0, tzinfo=UTC))

    with session_scope() as s:
        audit = PostgresAuditLog(s, clock)
        report = backfill(session=s, adapter=adapter, audit=audit, markets=markets,
                          start=start, end=end, seeds_csv=a.seeds)

    for m, r in report.markets.items():
        line = f"{m}: {r.sessions} sessions, {r.bars} bars, red={r.red} amber={r.amber}"
        if r.first_red:
            line += f" first_red={list(r.first_red)}"
        print(line)
    for pair, f in report.fx.items():
        print(f"fx {pair}: {f.rows} rows, missing_weekdays={f.missing_weekdays}"
              + (" EMPTY — FAILURE" if f.empty else ""))
    print(f"backfill {start}..{end} via {type(adapter).__name__}: "
          f"{'FAILURES PRESENT — inspect market.data_quality_gates' if report.failed else 'zero red gates'}")
    raise SystemExit(2 if report.failed else 0)


if __name__ == "__main__":
    main()
