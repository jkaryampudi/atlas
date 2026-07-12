"""History backfill (Phase 1 exit; deep-history per ADR-0004 condition 1): bars +
corporate actions + per-day quality gates + FX, over exchange-calendar sessions.

Usage: python -m atlas.dcp.market_data.backfill --years 2 --end 2026-07-10
       python -m atlas.dcp.market_data.backfill --from 2010-01-01 --end 2026-07-10
`--end` is explicit so runs are deterministic and replayable (no wall clock).
`--from` is the deep-history mode (full vendor history after the subscription
upgrade); without it `--years` keeps the ADR-0004 default window. Vendor: EODHD
when ATLAS_EODHD_API_KEY is set; the fixture adapter otherwise.

Adjustment convention (unchanged, deliberate): bars are fetched from the vendor's
raw OHLC fields and stored RAW, with splits recorded in `market.corporate_actions`
— adjustment happens on read via `adjustment.adjust_for_splits`. The vendor's
`adjusted_close` is NOT stored: it is retroactively rewritten by every future
split/dividend, so storing it would silently mutate history under an append-only
audit regime, while raw close + recorded actions replay deterministically. A deep
backfill keeps the exact same convention; only the window length changes.

Deep windows are fetched in bounded chunks (CHUNK_DAYS per vendor request) so a
~16-year range never rides on one giant response; splits are still one small
range request per instrument. Re-running any window is idempotent: bars, splits,
gates and FX all upsert on their natural keys (ON CONFLICT), so a resumed or
repeated deep run double-writes nothing.

Gates use quality rules v1.2: a symbol is expected only from its inception (its
earliest STORED bar — see quality.inception_map), so a 2010 start does not paint
dishonest REDs for instruments listed later (NDIA ~2019, INDA 2012, AVGO IPO
2009); a LISTED instrument missing bars is exactly as RED as before. A RED gate
is a reportable result; the exit criterion is zero red gates on a clean day, not
zero red gates by construction.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
from atlas.dcp.market_data.quality import evaluate_gate, inception_map

ROOT = Path(__file__).resolve().parents[3]

DEFAULT_YEARS = 2.0  # ADR-0004 standing default window when --from is not given

# One vendor request never spans more than ~5 years of dailies (~1260 rows).
# EODHD can serve full history in one response, but bounded chunks keep each
# response small, keep a mid-run retry cheap, and keep memory flat on a
# 2010-onward run. Chunks are inclusive, contiguous and non-overlapping, so the
# union of chunked fetches is exactly the single-range fetch.
CHUNK_DAYS = 1826


def chunk_windows(start: date, end: date, max_days: int = CHUNK_DAYS) -> list[tuple[date, date]]:
    """Split inclusive [start, end] into inclusive, contiguous, non-overlapping
    windows of at most max_days calendar days each. Empty if start > end."""
    if max_days < 1:
        raise ValueError(f"max_days must be >= 1, got {max_days}")
    out: list[tuple[date, date]] = []
    lo = start
    while lo <= end:
        hi = min(lo + timedelta(days=max_days - 1), end)
        out.append((lo, hi))
        lo = hi + timedelta(days=1)
    return out


@dataclass(frozen=True)
class MarketBackfill:
    market: str
    sessions: int
    bars: int
    red: int
    amber: int
    first_red: tuple[str, ...]        # first few red dates, for the report
    inceptions: dict[str, date]       # symbol -> earliest STORED bar (rules v1.2);
    #                                   a symbol with no bars at all is absent here
    #                                   and gates red on every day (fail closed)


@dataclass(frozen=True)
class FxPairSummary:
    pair: str
    rows: int
    missing_weekdays: int  # weekdays with no vendor rate (FOREX holidays expected ~2-3/yr)
    empty: bool            # zero rows over the whole window — always a failure
    first_rate: date | None  # series inception: earliest STORED rate; weekdays
    #                          before it are not gaps (the feed did not exist yet),
    #                          weekdays after it missing ARE (rules v1.2 analogue)


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
        # splits: one small range request; bars: chunked so a deep window never
        # rides on one giant vendor response (see CHUNK_DAYS).
        for sp in adapter.fetch_splits(inst["symbol"], days[0], end):
            record_split(session, inst["id"], sp, source)
            explained_by_day[sp.action_date].add(inst["symbol"])
        for lo, hi in chunk_windows(days[0], end):
            for b in adapter.fetch_bars(inst["symbol"], lo, hi):
                if not lo <= b.bar_date <= hi:
                    continue  # a vendor bar outside its requested chunk would
                    #           double-count across chunk boundaries; drop it
                upsert_bar(session, inst["id"], b, source)
                bars_by_day[b.bar_date].append(b)
                n_bars += 1

    # Inception AFTER upserting this run's bars (same transaction): the deep
    # window's own writes extend each symbol's inception backward (rules v1.2).
    inceptions = inception_map(session, market)
    expected_symbols = frozenset(inst["symbol"] for inst in instruments)
    red = amber = 0
    first_red: list[str] = []
    for i, d in enumerate(days):
        window = {k: bars_by_day.get(k, []) for k in days[max(0, i - 1):i + 1]}
        gate = evaluate_gate(market=market, as_of=d, expected_days=[d],
                             bars_by_day=window,
                             explained_symbols=frozenset(explained_by_day.get(d, set())),
                             expected_symbols=expected_symbols,
                             inceptions=inceptions)
        write_gate(session, gate)
        if gate.status is GateStatus.RED:
            red += 1
            if len(first_red) < 5:
                first_red.append(d.isoformat())
        elif gate.status is GateStatus.AMBER:
            amber += 1

    return MarketBackfill(market=market, sessions=len(days), bars=n_bars,
                          red=red, amber=amber, first_red=tuple(first_red),
                          inceptions=inceptions)


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
    # previously had no effect on success reporting at all. v1.2: the series is
    # expected from its first STORED rate — weekdays before the pair existed on
    # the feed are not gaps; a weekday gap after inception still is.
    fx: dict[str, FxPairSummary] = {}
    weekdays = _weekdays(start, end)
    for base, quote in required_pairs(session):
        # chunked like bars: contiguous inclusive windows, so the merged series
        # is exactly the single-range fetch (no boundary weekday lost or doubled)
        series: dict[date, Decimal] = {}
        for lo, hi in chunk_windows(start, end):
            series.update(adapter.fetch_fx_series(base, quote, lo, hi))
        for d, rate in sorted(series.items()):
            upsert_rate(session, base=base, quote=quote, day=d, rate=rate,
                        source=type(adapter).__name__)
        first_rate: date | None = session.execute(text(
            "SELECT min(rate_date) FROM market.fx_rates_daily "
            "WHERE base = :b AND quote = :q"), {"b": base, "q": quote}).scalar()
        pair = f"{base}{quote}"
        fx[pair] = FxPairSummary(
            pair=pair, rows=len(series),
            missing_weekdays=sum(1 for d in weekdays
                                 if first_rate is not None and d >= first_rate
                                 and d not in series),
            empty=not series, first_rate=first_rate)

    report = BackfillReport(markets=results, fx=fx)
    audit.append(event_type="market.backfill.completed", entity_type="market",
                 entity_id=",".join(markets), actor_type="scheduler", actor_id="backfill",
                 payload={"start": start.isoformat(), "end": end.isoformat(),
                          "fx": {p: {"rows": f.rows, "missing_weekdays": f.missing_weekdays,
                                     "empty": f.empty,
                                     "first_rate": f.first_rate.isoformat()
                                     if f.first_rate else None} for p, f in fx.items()},
                          "markets": {m: {"sessions": r.sessions, "bars": r.bars,
                                          "red": r.red, "amber": r.amber,
                                          "first_red": list(r.first_red),
                                          "inceptions": {s: d.isoformat() for s, d
                                                         in sorted(r.inceptions.items())}}
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
    p.add_argument("--years", type=float, default=None,
                   help=f"window length back from --end (default {DEFAULT_YEARS}; "
                        "ADR-0004 window)")
    p.add_argument("--from", dest="start", default=None,
                   help="explicit ISO start date for deep-history windows "
                        "(mutually exclusive with --years)")
    p.add_argument("--end", required=True,
                   help="last calendar day (ISO), explicit for determinism")
    p.add_argument("--market", action="append", dest="markets",
                   help="restrict to a market (repeatable); default: all in seeds")
    p.add_argument("--seeds", type=Path, default=ROOT / "seeds" / "instruments_seed.csv")
    a = p.parse_args()

    if a.start is not None and a.years is not None:
        p.error("--from and --years are mutually exclusive; pick one window spec")
    end = date.fromisoformat(a.end)
    if a.start is not None:
        start = date.fromisoformat(a.start)
        if start > end:
            p.error(f"--from {start} is after --end {end}")
    else:
        start = end - timedelta(days=round((a.years or DEFAULT_YEARS) * 365.25))
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
        for sym, d in sorted(r.inceptions.items()):
            print(f"  inception {sym}: {d.isoformat()}")
    for pair, f in report.fx.items():
        print(f"fx {pair}: {f.rows} rows, missing_weekdays={f.missing_weekdays}, "
              f"first_rate={f.first_rate}"
              + (" EMPTY — FAILURE" if f.empty else ""))
    print(f"backfill {start}..{end} via {type(adapter).__name__}: "
          f"{'FAILURES PRESENT — inspect market.data_quality_gates' if report.failed else 'zero red gates'}")
    raise SystemExit(2 if report.failed else 0)


if __name__ == "__main__":
    main()
