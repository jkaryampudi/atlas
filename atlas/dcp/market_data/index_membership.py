"""Point-in-time index membership (the definitive survivorship test's data leg).

EODHD's index fundamentals (`GSPC.INDX`) carry `HistoricalTickerComponents`:
one row per ticker that is or ever was a constituent, with nullable
StartDate/EndDate and IsActiveNow/IsDelisted flags (prong-B probe, appendix of
docs/reports/xsmom-etf-crosscheck-2026-07.md). This module fetches that table
once, persists it verbatim to `validation.index_membership` (migration 0015 —
a SEALED validation-plane table, no agent grants), and owns the fail-closed
membership rule every consumer must share.

MEMBERSHIP-INTERVAL RULE (fail closed, single source of truth):
a ticker is a member of the index on day D iff its row is USABLE and

    (start_date IS NULL OR start_date <= D) AND
    (end_date   IS NULL OR end_date   >  D)

Usability handles the vendor's recorded gaps:
- start_date present -> usable (the interval is knowable);
- start_date NULL and is_active_now -> usable, treated as a member from the
  window start: a long-standing CURRENT member whose join the vendor never
  recorded — exact for any evaluation window beginning after its true join;
- start_date NULL and NOT is_active_now -> EXCLUDED ENTIRELY (unknowable
  interval = fail closed). These rows also demonstrably carry ticker-reuse
  confusion (e.g. the vendor's 'ALTR' row names Altair Engineering against
  Altera's 2015 index exit), so no interval inferred from them can be
  trusted. They are counted and reported, split delisted vs departed.

Because EndDate coverage is sparse before ~2012 (probe: earliest 2008-09-16,
only a handful before 2012), any evaluation window over this table must start
no earlier than 2012-07-01 — WINDOW_START below; membership before that is
unreliable and the runner refuses it.

Seeding + backfill reuse the validation-only instrument mechanism verbatim
(validation_universe.seed_validation_instruments -> is_active = FALSE,
invisible to the scanner/desk/gates; backfill.backfill_symbols -> no quality
gates, no FX). The backfill driver here is FAIL-SOFT PER SYMBOL: delisted
names may 404 at the vendor; each failure is recorded and reported, never
fatal — honest coverage numbers are the deliverable.

Usage:
  python -m atlas.dcp.market_data.index_membership fetch
  python -m atlas.dcp.market_data.index_membership seed
  python -m atlas.dcp.market_data.index_membership backfill
"""
from __future__ import annotations

import argparse
import csv
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[3]
INDEX_CODE = "GSPC.INDX"
MEMBER_SEEDS = ROOT / "seeds" / "validation_sp500_members.csv"

# Documented reliability bound: vendor EndDates are sparse before ~2012, so no
# evaluation window over this membership table may start earlier than this.
WINDOW_START = date(2012, 7, 1)
# Price history starts two years earlier so the first rebalance already has a
# full 252-session seasoning/formation window behind it.
PRICE_START = date(2010, 7, 1)
PRICE_END = date(2026, 7, 10)


@dataclass(frozen=True)
class MembershipRow:
    index_code: str
    ticker: str
    name: str
    start_date: date | None
    end_date: date | None
    is_active_now: bool
    is_delisted: bool


def parse_membership(payload: Mapping[str, object], *,
                     index_code: str = INDEX_CODE) -> list[MembershipRow]:
    """Vendor fundamentals document -> membership rows, verbatim (nulls kept).
    Refuses a payload without HistoricalTickerComponents or with duplicate
    ticker codes — the PK assumes one interval per ticker, and a silent
    last-row-wins would corrupt the reconstruction."""
    hist = payload.get("HistoricalTickerComponents")
    if hist is None:
        raise ValueError(f"{index_code}: payload has no HistoricalTickerComponents "
                         "— point-in-time membership is not available")
    entries: list[object] = (list(hist.values()) if isinstance(hist, dict)
                             else list(hist) if isinstance(hist, list) else [])
    if not entries:
        raise ValueError(f"{index_code}: HistoricalTickerComponents is empty")
    rows: list[MembershipRow] = []
    seen: set[str] = set()
    for e in entries:
        if not isinstance(e, Mapping):
            raise ValueError(f"{index_code}: malformed component entry {e!r}")
        code = str(e["Code"])
        if code in seen:
            raise ValueError(f"{index_code}: duplicate ticker code {code!r} in "
                             "HistoricalTickerComponents — refusing ambiguity")
        seen.add(code)
        sd, ed = e.get("StartDate"), e.get("EndDate")
        rows.append(MembershipRow(
            index_code=index_code, ticker=code, name=str(e.get("Name") or ""),
            start_date=date.fromisoformat(str(sd)) if sd else None,
            end_date=date.fromisoformat(str(ed)) if ed else None,
            is_active_now=bool(e.get("IsActiveNow")),
            is_delisted=bool(e.get("IsDelisted"))))
    return rows


def usable(row: MembershipRow) -> bool:
    """Fail-closed usability (see module docstring): a null StartDate is usable
    ONLY for a current member (member-from-window-start); a null StartDate on a
    departed or delisted row is an unknowable interval and is excluded."""
    return row.start_date is not None or row.is_active_now


def is_member_on(row: MembershipRow, day: date) -> bool:
    """THE membership-interval rule: usable AND (start IS NULL OR start <= D)
    AND (end IS NULL OR end > D). End-exclusive: on its removal date a ticker
    is no longer a member."""
    if not usable(row):
        return False
    return ((row.start_date is None or row.start_date <= day)
            and (row.end_date is None or row.end_date > day))


def member_in_window(row: MembershipRow, window_start: date,
                     window_end: date) -> bool:
    """Member on at least one day of [window_start, window_end] (same rule)."""
    if not usable(row):
        return False
    return ((row.start_date is None or row.start_date <= window_end)
            and (row.end_date is None or row.end_date > window_start))


@dataclass(frozen=True)
class MembershipPartition:
    usable: tuple[MembershipRow, ...]
    excluded_null_start_delisted: tuple[MembershipRow, ...]
    excluded_null_start_departed: tuple[MembershipRow, ...]  # not active, not delisted


def partition_membership(rows: Sequence[MembershipRow]) -> MembershipPartition:
    """Split rows by the fail-closed usability rule, keeping the excluded
    buckets separately countable — the report must state exactly what the
    reconstruction refused and why."""
    ok: list[MembershipRow] = []
    ex_delisted: list[MembershipRow] = []
    ex_departed: list[MembershipRow] = []
    for r in rows:
        if usable(r):
            ok.append(r)
        elif r.is_delisted:
            ex_delisted.append(r)
        else:
            ex_departed.append(r)
    return MembershipPartition(usable=tuple(ok),
                               excluded_null_start_delisted=tuple(ex_delisted),
                               excluded_null_start_departed=tuple(ex_departed))


def replace_membership(session: Session, rows: Sequence[MembershipRow], *,
                       index_code: str = INDEX_CODE,
                       fetched_at: datetime) -> int:
    """Persist a fetched snapshot wholesale: delete-then-insert per index_code.
    The table is a vendor snapshot cache (each fetch is audited by the caller),
    not an append-only record — a re-fetch must not leave rows the vendor no
    longer serves."""
    if not rows:
        raise ValueError("refusing to persist an empty membership snapshot")
    if any(r.index_code != index_code for r in rows):
        raise ValueError("rows carry a different index_code than requested")
    session.execute(text(
        "DELETE FROM validation.index_membership WHERE index_code = :ic"),
        {"ic": index_code})
    session.execute(text(
        "INSERT INTO validation.index_membership "
        "(index_code, ticker, name, start_date, end_date, is_active_now, "
        " is_delisted, fetched_at) "
        "VALUES (:index_code, :ticker, :name, :start_date, :end_date, "
        "        :is_active_now, :is_delisted, :fetched_at)"),
        [{"index_code": r.index_code, "ticker": r.ticker, "name": r.name,
          "start_date": r.start_date, "end_date": r.end_date,
          "is_active_now": r.is_active_now, "is_delisted": r.is_delisted,
          "fetched_at": fetched_at} for r in rows])
    return len(rows)


def load_membership(session: Session, *,
                    index_code: str = INDEX_CODE) -> list[MembershipRow]:
    rows = session.execute(text(
        "SELECT index_code, ticker, name, start_date, end_date, is_active_now, "
        "is_delisted FROM validation.index_membership "
        "WHERE index_code = :ic ORDER BY ticker"), {"ic": index_code}).all()
    return [MembershipRow(index_code=r.index_code, ticker=r.ticker,
                          name=r.name or "", start_date=r.start_date,
                          end_date=r.end_date, is_active_now=r.is_active_now,
                          is_delisted=r.is_delisted) for r in rows]


def symbols_with_bars(session: Session) -> set[str]:
    """Symbols already holding vendor bars (any activity state) — the dedupe
    set for seeding: fetching a vendor code that already has a stored series
    would double-ingest the identical series (EODHD serves exactly one series
    per code, so symbol-level dedupe is equivalent to vendor-code dedupe)."""
    return {r.symbol for r in session.execute(text(
        "SELECT DISTINCT i.symbol FROM market.instruments i "
        "JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
        "WHERE pb.source = 'EodhdAdapter'"))}


def write_member_seeds_csv(rows: Sequence[MembershipRow], path: Path) -> int:
    """Seeds-shaped CSV for the validation-instrument mechanism. Exchange 'US'
    is EODHD's own umbrella venue code (adapters/eodhd._US_EXCHANGES): the
    vendor addresses every one of these tickers as <CODE>.US, and the true
    historical venue of a delisted name is unknowable from this payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "exchange", "market", "instrument_type", "name",
                    "sector_gics", "currency", "economic_exposure"])
        for r in sorted(rows, key=lambda x: x.ticker):
            w.writerow([r.ticker, "US", "US", "stock", r.name, "", "USD", "US"])
    return len(rows)


@dataclass(frozen=True)
class MemberBackfillOutcome:
    symbol: str
    delisted: bool
    ok: bool
    bars: int
    inception: date | None
    error: str  # '' when ok


def _cli_fetch(index_code: str) -> None:
    from atlas.core.audit_repo import PostgresAuditLog
    from atlas.core.clock import SystemClock
    from atlas.core.config import get_settings
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter

    settings = get_settings()
    if not settings.eodhd_api_key:
        raise SystemExit("ATLAS_EODHD_API_KEY is not set — membership fetch "
                         "needs the real vendor (no fixture equivalent)")
    # explicit vendor-code mode: no symbol map, the index code is passed as-is
    adapter = EodhdAdapter(settings.eodhd_api_key)
    payload = adapter.fetch_fundamentals(index_code)
    rows = parse_membership(payload, index_code=index_code)
    part = partition_membership(rows)
    clock = SystemClock()
    fetched_at = clock.now()
    with session_scope() as s:
        n = replace_membership(s, rows, index_code=index_code, fetched_at=fetched_at)
        PostgresAuditLog(s, clock).append(
            event_type="validation.index_membership.fetched",
            entity_type="market", entity_id=index_code,
            actor_type="human", actor_id="index_membership",
            payload={"rows": n, "usable": len(part.usable),
                     "excluded_null_start_delisted":
                         len(part.excluded_null_start_delisted),
                     "excluded_null_start_departed":
                         len(part.excluded_null_start_departed),
                     "fetched_at": fetched_at.isoformat(),
                     "rule": "fail-closed (module docstring): null StartDate "
                             "usable only for current members"})
    print(f"{index_code}: persisted {n} membership rows "
          f"(usable={len(part.usable)}, excluded null-start+delisted="
          f"{len(part.excluded_null_start_delisted)}, excluded null-start+"
          f"departed={len(part.excluded_null_start_departed)})")


def _cli_seed(index_code: str, window_start: date, window_end: date,
              csv_path: Path) -> None:
    from atlas.core.audit_repo import PostgresAuditLog
    from atlas.core.clock import SystemClock
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.validation_universe import seed_validation_instruments

    with session_scope() as s:
        rows = load_membership(s, index_code=index_code)
        if not rows:
            raise SystemExit(f"no membership rows for {index_code} — run "
                             "`index_membership fetch` first")
        needed = [r for r in rows if member_in_window(r, window_start, window_end)]
        have = symbols_with_bars(s)
        missing = [r for r in needed if r.ticker not in have]
        write_member_seeds_csv(missing, csv_path)
        res = seed_validation_instruments(s, csv_path)
        PostgresAuditLog(s, SystemClock()).append(
            event_type="validation.index_membership.seeded",
            entity_type="market", entity_id=index_code,
            actor_type="human", actor_id="index_membership",
            payload={"window": f"{window_start}..{window_end}",
                     "members_in_window": len(needed),
                     "already_have_bars": len(needed) - len(missing),
                     "seeded_csv": str(csv_path),
                     "inserted": len(res.inserted),
                     "already_present": len(res.already_present),
                     "is_active": False})
    print(f"{index_code} members in {window_start}..{window_end}: {len(needed)} "
          f"({len(needed) - len(missing)} already hold bars)")
    print(f"seeded {len(res.inserted)} validation instruments "
          f"({len(res.already_present)} already present) from {csv_path}")


def _cli_backfill(index_code: str, start: date, end: date,
                  pause_s: float) -> None:
    from atlas.core.audit_repo import PostgresAuditLog
    from atlas.core.clock import FrozenClock
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters import adapter_from_settings
    from atlas.dcp.market_data.backfill import backfill_symbols

    adapter = adapter_from_settings(
        fixtures_root=ROOT / "tests" / "fixtures",
        seeds_csv=ROOT / "seeds" / "instruments_seed.csv",
        extra_seeds_csv=MEMBER_SEEDS if MEMBER_SEEDS.exists() else None)
    clock = FrozenClock(datetime(end.year, end.month, end.day, 22, 0, tzinfo=UTC))

    with session_scope() as s:
        members = {r.ticker: r for r in load_membership(s, index_code=index_code)}
        todo = sorted(sym for sym in members
                      if s.execute(text(
                          "SELECT 1 FROM market.instruments WHERE symbol = :s "
                          "AND NOT is_active AND exchange = 'US'"),
                          {"s": sym}).scalar() is not None
                      and s.execute(text(
                          "SELECT 1 FROM market.price_bars_daily pb "
                          "JOIN market.instruments i ON i.id = pb.instrument_id "
                          "WHERE i.symbol = :s LIMIT 1"), {"s": sym}).scalar() is None)
    print(f"backfilling {len(todo)} seeded member symbols {start}..{end} "
          f"via {type(adapter).__name__} (fail-soft per symbol)")

    outcomes: list[MemberBackfillOutcome] = []
    for k, sym in enumerate(todo, start=1):
        delisted = members[sym].is_delisted
        try:
            with session_scope() as s:
                rep = backfill_symbols(session=s, adapter=adapter,
                                       audit=PostgresAuditLog(s, clock),
                                       symbols=[sym], start=start, end=end)
                sb = rep.symbols[0]
                out = MemberBackfillOutcome(
                    symbol=sym, delisted=delisted, ok=sb.inception is not None,
                    bars=sb.bars, inception=sb.inception,
                    error="" if sb.inception is not None else "vendor returned no bars")
        except Exception as exc:  # fail-soft: recorded, never fatal
            out = MemberBackfillOutcome(symbol=sym, delisted=delisted, ok=False,
                                        bars=0, inception=None,
                                        error=f"{type(exc).__name__}: {exc}")
        outcomes.append(out)
        status = "ok" if out.ok else f"FAILED ({out.error})"
        print(f"[{k}/{len(todo)}] {sym}{' [delisted]' if delisted else ''}: "
              f"{out.bars} bars, inception={out.inception} — {status}",
              flush=True)
        time.sleep(pause_s)

    failed = [o for o in outcomes if not o.ok]
    with session_scope() as s:
        PostgresAuditLog(s, clock).append(
            event_type="validation.index_membership.backfilled",
            entity_type="market", entity_id=index_code,
            actor_type="scheduler", actor_id="index_membership",
            payload={"start": start.isoformat(), "end": end.isoformat(),
                     "symbols": len(todo), "ok": len(outcomes) - len(failed),
                     "failed": {o.symbol: o.error for o in failed},
                     "failed_delisted": sum(1 for o in failed if o.delisted)})
    print(f"backfill complete: {len(outcomes) - len(failed)}/{len(todo)} stored, "
          f"{len(failed)} failed ({sum(1 for o in failed if o.delisted)} delisted)")
    for o in failed:
        print(f"  FAILED {o.symbol}{' [delisted]' if o.delisted else ''}: {o.error}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Point-in-time index membership: fetch/seed/backfill "
                    "(validation plane; sealed schema)")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="fetch HistoricalTickerComponents once "
                                     "and persist the snapshot")
    f.add_argument("--index", default=INDEX_CODE)
    se = sub.add_parser("seed", help="seed missing member tickers as "
                                     "validation-only instruments")
    se.add_argument("--index", default=INDEX_CODE)
    se.add_argument("--window-start", type=date.fromisoformat, default=WINDOW_START)
    se.add_argument("--window-end", type=date.fromisoformat, default=PRICE_END)
    se.add_argument("--csv", type=Path, default=MEMBER_SEEDS)
    b = sub.add_parser("backfill", help="fail-soft per-symbol bar backfill of "
                                        "seeded member tickers")
    b.add_argument("--index", default=INDEX_CODE)
    b.add_argument("--from", dest="start", type=date.fromisoformat,
                   default=PRICE_START)
    b.add_argument("--end", type=date.fromisoformat, default=PRICE_END)
    b.add_argument("--pause", type=float, default=0.05,
                   help="seconds between symbols (vendor politeness)")
    a = p.parse_args()

    if a.cmd == "fetch":
        _cli_fetch(a.index)
    elif a.cmd == "seed":
        _cli_seed(a.index, a.window_start, a.window_end, a.csv)
    else:
        _cli_backfill(a.index, a.start, a.end, a.pause)


if __name__ == "__main__":
    main()
