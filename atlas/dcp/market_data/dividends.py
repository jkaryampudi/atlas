"""Cash-dividend history backfill (board memo 2026-07, item 1).

WHY: ADR-0009's binding benchmark is SPY TOTAL RETURN, and no dividend was
ingested anywhere in the system — every prior verdict was scored price-return
vs price-return. This module fetches EODHD dividend history (GET /api/div/
{code}?from=..&to=..) for every requested symbol and records it in
market.corporate_actions with action_type='dividend' — the 0001 CHECK
constraint already permits it and the table carries dedicated `amount` and
`currency` columns, so NO new table or migration is needed. `ratio` stays NULL
(split semantics only).

Storage convention (identical to bars — backfill.py module docstring): the RAW
declared cash per share is stored (the vendor's `unadjustedValue`), never the
vendor's retroactively-rewritten split-adjusted `value`; adjustment happens on
read via total_return.adjust_dividends_for_splits, exactly as prices adjust
via adjustment.adjust_for_splits. Re-runs are idempotent on the natural key
(instrument_id, action_date, action_type).

Fail-soft per symbol: a vendor error for one ticker (delisted names 404
sometimes) is RECORDED and skipped, never fatal to the batch — but the report
distinguishes three honest states per symbol:
  ok      — fetched, >= 1 dividend stored
  empty   — fetched successfully, vendor has none (many names never paid; NORMAL)
  failed  — the fetch itself errored (coverage hole; listed verbatim)

No quality gates are written (gate coverage is a tradable-universe bar
contract — see backfill.backfill_symbols) and no FX is touched.

Usage:
  python -m atlas.dcp.market_data.dividends --from 2010-01-01 --end 2026-07-10
  python -m atlas.dcp.market_data.dividends --symbols SPY,AAPL --from ... --end ...
Default symbol set: every instrument with stored vendor bars (the tradable
universe + validation instruments + PIT members — anything a panel can load).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.ingest import record_dividend

ROOT = Path(__file__).resolve().parents[3]

# Matches the deep-history bar backfill window (PRICE_START era): dividends
# before the first stored bar can never be reinvested and are dropped on read.
DIVIDEND_START = date(2010, 1, 1)


@dataclass(frozen=True)
class SymbolDividends:
    symbol: str
    status: str            # "ok" | "empty" | "failed"
    rows: int              # dividends returned by THIS fetch (pre-dedup)
    error: str = ""        # verbatim, for status == "failed"


@dataclass(frozen=True)
class DividendsBackfillReport:
    symbols: tuple[SymbolDividends, ...]

    @property
    def ok(self) -> int:
        return sum(1 for s in self.symbols if s.status == "ok")

    @property
    def empty(self) -> int:
        return sum(1 for s in self.symbols if s.status == "empty")

    @property
    def failed(self) -> tuple[SymbolDividends, ...]:
        return tuple(s for s in self.symbols if s.status == "failed")


def symbols_with_stored_bars(session: Session) -> list[str]:
    """Every symbol with vendor bars — the honest ingest target: any series a
    panel loader can serve must carry its dividends too."""
    return [str(r[0]) for r in session.execute(text(
        "SELECT DISTINCT i.symbol FROM market.instruments i "
        "JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
        "WHERE pb.source = 'EodhdAdapter' ORDER BY i.symbol"))]


def backfill_dividends(*, session: Session, adapter: MarketDataAdapter,
                       audit: PostgresAuditLog, symbols: list[str],
                       start: date, end: date) -> DividendsBackfillReport:
    """Fetch + record dividend history for EXACTLY the named symbols,
    fail-soft per symbol, one audit event with the full coverage picture.
    A symbol with no instrument row refuses loudly — seeding is a separate,
    deliberate step, never a side effect here (the backfill_symbols rule)."""
    rows = session.execute(text(
        "SELECT id, symbol FROM market.instruments WHERE symbol = ANY(:syms)"),
        {"syms": symbols}).mappings().all()
    by_symbol = {r["symbol"]: r["id"] for r in rows}
    unknown = sorted(set(symbols) - set(by_symbol))
    if unknown:
        raise ValueError(f"unknown symbol(s) {unknown} — seed instruments first")

    source = type(adapter).__name__
    out: list[SymbolDividends] = []
    for sym in symbols:
        try:
            divs = adapter.fetch_dividends(sym, start, end)
        except Exception as exc:  # fail-soft: record verbatim, keep going
            out.append(SymbolDividends(symbol=sym, status="failed", rows=0,
                                       error=f"{type(exc).__name__}: {exc}"))
            continue
        for dv in divs:
            record_dividend(session, by_symbol[sym], dv, source)
        out.append(SymbolDividends(symbol=sym,
                                   status="ok" if divs else "empty",
                                   rows=len(divs)))

    report = DividendsBackfillReport(symbols=tuple(out))
    audit.append(
        event_type="market.dividends.backfill.completed", entity_type="market",
        entity_id=f"{len(symbols)} symbols", actor_type="scheduler",
        actor_id="dividends_backfill",
        payload={"start": start.isoformat(), "end": end.isoformat(),
                 "symbols_requested": len(symbols),
                 "with_dividends": report.ok,
                 "fetched_none": report.empty,
                 "fetch_failed": [s.symbol for s in report.failed],
                 "fetch_failed_errors": {s.symbol: s.error
                                         for s in report.failed},
                 "rows_fetched": sum(s.rows for s in report.symbols),
                 "gates_written": False,
                 "convention": "raw declared cash per share (unadjustedValue); "
                               "split adjustment happens on read, the bars "
                               "convention"})
    return report


def symbol_map_from_instruments(session: Session,
                                symbols: list[str]) -> dict[str, str]:
    """Canonical symbol -> EODHD vendor code straight from market.instruments
    (symbol, exchange) — the fallback for instruments that predate or outlive
    the seeds CSVs (first ingest run: ALGM and PEGA carry stored bars but
    appear in no current seeds file, so the seeds-derived map refused them).
    Same strict rules as the seeds maps: unknown exchanges fail loudly in
    vendor_symbol; a symbol listed on two exchanges refuses."""
    from atlas.dcp.market_data.adapters.eodhd import vendor_symbol

    out: dict[str, str] = {}
    for r in session.execute(text(
            "SELECT symbol, exchange FROM market.instruments "
            "WHERE symbol = ANY(:syms) ORDER BY symbol, exchange"),
            {"syms": symbols}):
        code = vendor_symbol(r.symbol, r.exchange)
        if r.symbol in out and out[r.symbol] != code:
            raise ValueError(f"dual-listed symbol {r.symbol!r}: {out[r.symbol]} "
                             f"vs {code} — symbol-map keys must be unambiguous")
        out[r.symbol] = code
    return out


def _merged_symbol_map() -> dict[str, str]:
    """Canonical symbol -> EODHD vendor code across EVERY seeds surface the
    project has (instrument seeds, signed universe manifest, validation ETFs,
    PIT member seeds) — the dividends target set spans all of them. Same
    strict collision rules as adapter_from_settings; identical duplicates are
    tolerated (SPY appears in several)."""
    from atlas.dcp.market_data.adapters.eodhd import (symbol_map_from_seeds,
                                                       symbol_map_from_universe)
    from atlas.dcp.market_data.index_membership import MEMBER_SEEDS
    from atlas.dcp.market_data.validation_universe import VALIDATION_SEEDS

    sources: list[dict[str, str]] = [
        symbol_map_from_seeds(ROOT / "seeds" / "instruments_seed.csv")]
    universe_json = ROOT / "seeds" / "universe.json"
    if universe_json.exists():
        sources.append(symbol_map_from_universe(universe_json))
    for extra in (VALIDATION_SEEDS, MEMBER_SEEDS):
        if extra.exists():
            sources.append(symbol_map_from_seeds(extra))
    merged: dict[str, str] = {}
    for m in sources:
        for sym, code in m.items():
            if sym in merged and merged[sym] != code:
                raise ValueError(f"symbol map conflict for {sym!r}: "
                                 f"{merged[sym]} vs {code}")
            merged[sym] = code
    return merged


def main() -> None:
    from atlas.core.clock import FrozenClock
    from atlas.core.config import get_settings
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter
    from atlas.dcp.market_data.adapters.fixture import FixtureAdapter

    p = argparse.ArgumentParser(
        description="Backfill cash-dividend history into market.corporate_actions")
    p.add_argument("--from", dest="start", default=DIVIDEND_START.isoformat(),
                   help=f"ISO start (default {DIVIDEND_START})")
    p.add_argument("--end", required=True,
                   help="last calendar day (ISO), explicit for determinism")
    p.add_argument("--symbols", default=None,
                   help="comma-separated symbols; default: every symbol with "
                        "stored vendor bars")
    a = p.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if start > end:
        p.error(f"--from {start} is after --end {end}")
    clock = FrozenClock(datetime(end.year, end.month, end.day, 22, 0, tzinfo=UTC))

    settings = get_settings()
    with session_scope() as s:
        symbols = ([x.strip() for x in a.symbols.split(",") if x.strip()]
                   if a.symbols else symbols_with_stored_bars(s))
        if not symbols:
            raise SystemExit("no symbols to fetch — run the bars backfill first")
        if settings.eodhd_api_key:
            # seeds-derived entries first; instruments-derived fallback covers
            # symbols the seeds CSVs no longer carry (seeds win on overlap —
            # they encode deliberate vendor-code decisions)
            symbol_map = _merged_symbol_map()
            unmapped = [x for x in symbols if x not in symbol_map]
            if unmapped:
                symbol_map = {**symbol_map_from_instruments(s, unmapped),
                              **symbol_map}
            adapter: MarketDataAdapter = EodhdAdapter(
                settings.eodhd_api_key, symbol_map=symbol_map)
        else:
            adapter = FixtureAdapter(ROOT / "tests" / "fixtures")
        report = backfill_dividends(session=s, adapter=adapter,
                                    audit=PostgresAuditLog(s, clock),
                                    symbols=symbols, start=start, end=end)

    for sd in report.failed:
        print(f"FAILED {sd.symbol}: {sd.error}")
    print(f"dividends backfill {start}..{end} via {type(adapter).__name__}: "
          f"{len(symbols)} symbols — {report.ok} with dividends, "
          f"{report.empty} fetched-none (normal), {len(report.failed)} failed; "
          f"{sum(s.rows for s in report.symbols)} rows fetched")
    raise SystemExit(2 if report.failed else 0)


if __name__ == "__main__":
    main()
