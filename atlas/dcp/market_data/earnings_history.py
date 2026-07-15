"""Earnings::History ingest -> market.earnings_surprises (immutable FACTS).

The EODHD fundamentals document carries an ``Earnings.History`` block: one
entry per fiscal-period-end (the dict key, an ISO date), each with
``reportDate`` (announcement day), ``epsActual`` (reported per-share EPS),
``epsEstimate`` (pre-report consensus), ``epsDifference``, ``surprisePercent``
(split-neutral ratio), ``beforeAfterMarket`` and ``currency`` (shape probed
live 2026-07-15 against AAPL and the acquired-then-delisted ATVI — the latter
carries full history through its 2023 acquisition, so a survivorship-free
earnings panel is feasible).

WHAT IS STORED — completed quarters only. A row is stored iff BOTH epsActual
and epsEstimate are present numbers AND reportDate parses as a date. Future
fiscal periods (the vendor keeps forward rows with epsActual=null) and
estimate-less legacy rows are skipped — a surprise needs both legs.

APPEND-ONLY (immutability). Unlike the earnings *calendar* (a mutable forecast
store that upserts/deletes rescheduled dates), a completed report is a settled
historical fact. Ingestion is ON CONFLICT (instrument_id, fiscal_period_end)
DO NOTHING: a stored row is never overwritten, re-ingestion is idempotent, and
nothing this module does can rewrite history. Mirrors market.fundamentals.

FAIL-SOFT PER INSTRUMENT. A vendor fetch failure (delisted 404, transport
error) or a missing instrument row is recorded in ``failures`` and reported;
the run continues. Honest coverage counts are the deliverable.

``fetched_at`` comes from the injected clock (CLAUDE.md invariant 6). EPS
values are stored AS THE VENDOR PROVIDES THEM — EODHD Earnings::History is
backward-split-adjusted to the current share basis (the actual/estimate series
is continuous across splits), so the signal uses them directly with NO on-read
split adjustment (an earlier "adjust on read" assumption double-adjusted the
data; corrected after an adversarial audit 2026-07-15).

Usage:
  python -m atlas.dcp.market_data.earnings_history --symbols AAPL,MSFT,ATVI
  python -m atlas.dcp.market_data.earnings_history --members   # all PIT members
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.models import EARNINGS_WHEN_TIMES

ROOT = Path(__file__).resolve().parents[3]
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class EarningsSurprise:
    """One completed quarterly report — a settled fact. eps values are as the
    vendor provides them: backward-split-adjusted to the current basis (used
    directly, no on-read adjustment)."""
    symbol: str
    fiscal_period_end: date        # vendor Earnings::History key
    report_date: date              # announcement day; the look-ahead anchor
    eps_actual: Decimal
    eps_estimate: Decimal
    surprise_pct: Decimal | None   # vendor split-neutral ratio (secondary signal)
    before_after_market: str | None
    currency: str | None = None


def _decimal(value: object) -> Decimal | None:
    """A finite number (int/float/Decimal, or a plain-decimal string) -> Decimal,
    else None. Rejects bool, None, NaN/inf and free text — EPS may be negative
    or zero (a loss, a nil-estimate quarter), so no sign/zero constraint."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _flag(value: object) -> str | None:
    """Vendor timing flag admitted only inside the closed vocabulary
    (models.EARNINGS_WHEN_TIMES); anything else becomes None."""
    return value if value in EARNINGS_WHEN_TIMES else None


def _currency(value: object) -> str | None:
    """ISO-4217-shaped currency code, else None."""
    return value if isinstance(value, str) and _CURRENCY_CODE.fullmatch(value) else None


def parse_earnings_history(payload: dict[str, object], symbol: str,
                           ) -> list[EarningsSurprise]:
    """Vendor fundamentals document -> completed-quarter surprise rows.

    Reads ONLY Earnings.History; every field passes a typed choke point.
    Returns rows sorted by fiscal_period_end. A payload with no usable
    Earnings.History block yields an empty list (a valid answer — ETFs and
    never-reporting instruments have none)."""
    earnings = payload.get("Earnings")
    hist = earnings.get("History") if isinstance(earnings, dict) else None
    if not isinstance(hist, dict):
        return []
    general = payload.get("General")
    currency_doc = _currency(general.get("CurrencyCode")
                             if isinstance(general, dict) else None)
    out: list[EarningsSurprise] = []
    for key, row in hist.items():
        if not isinstance(row, dict):
            continue
        try:
            fpe = date.fromisoformat(str(key))
        except ValueError:
            continue
        try:
            report_date = date.fromisoformat(str(row.get("reportDate")))
        except ValueError:
            continue  # no announcement date -> not point-in-time anchorable
        actual = _decimal(row.get("epsActual"))
        estimate = _decimal(row.get("epsEstimate"))
        if actual is None or estimate is None:
            continue  # not a completed report (need both legs for a surprise)
        out.append(EarningsSurprise(
            symbol=symbol, fiscal_period_end=fpe, report_date=report_date,
            eps_actual=actual, eps_estimate=estimate,
            surprise_pct=_decimal(row.get("surprisePercent")),
            before_after_market=_flag(row.get("beforeAfterMarket")),
            currency=_currency(row.get("currency")) or currency_doc))
    out.sort(key=lambda r: r.fiscal_period_end)
    return out


def store_surprises(session: Session, instrument_id: object,
                    rows: list[EarningsSurprise], *, fetched_at: datetime,
                    source: str) -> int:
    """Append-only insert of completed reports. ON CONFLICT DO NOTHING on the
    natural key (instrument_id, fiscal_period_end): a settled fact is never
    overwritten and re-ingestion is idempotent. Returns rows newly inserted."""
    inserted = 0
    for r in rows:
        res = session.execute(text(
            "INSERT INTO market.earnings_surprises "
            "(instrument_id, fiscal_period_end, report_date, eps_actual, "
            " eps_estimate, surprise_pct, currency, before_after_market, "
            " source, fetched_at) "
            "VALUES (:iid, :fpe, :rd, :a, :e, :sp, :cur, :baf, :src, :fa) "
            "ON CONFLICT (instrument_id, fiscal_period_end) DO NOTHING "
            "RETURNING id"),
            {"iid": instrument_id, "fpe": r.fiscal_period_end,
             "rd": r.report_date, "a": r.eps_actual, "e": r.eps_estimate,
             "sp": r.surprise_pct, "cur": r.currency,
             "baf": r.before_after_market, "src": source, "fa": fetched_at})
        inserted += 1 if res.first() is not None else 0
    return inserted


@dataclass(frozen=True)
class EarningsHistoryIngest:
    fetched: tuple[str, ...]     # vendor doc parsed and rows stored/idempotent
    stored: int                  # rows newly inserted this run (append-only)
    empty: tuple[str, ...]       # no completed quarters in the vendor doc
    failed: tuple[str, ...]      # vendor fetch failed / no instrument row


def ingest_earnings_history(session: Session, adapter: MarketDataAdapter,
                            symbols: list[str], *, now: datetime,
                            failures: list[str]) -> EarningsHistoryIngest:
    """Fetch + store completed earnings surprises for each symbol. Fail-soft
    per instrument: a missing instrument row or a vendor failure is recorded
    in ``failures`` (alertable, exit 2 upstream) and the run continues."""
    source = type(adapter).__name__
    fetched: list[str] = []
    empty: list[str] = []
    failed: list[str] = []
    stored = 0
    for symbol in symbols:
        iid = session.execute(text(
            "SELECT id FROM market.instruments WHERE symbol = :s"),
            {"s": symbol}).scalar()
        if iid is None:
            failures.append(f"earnings_history {symbol}: no instrument row")
            failed.append(symbol)
            continue
        try:
            payload = adapter.fetch_fundamentals(symbol)
        except Exception as exc:  # vendor failure: recorded, not fatal
            failures.append(f"earnings_history {symbol}: vendor fetch failed: {exc}")
            failed.append(symbol)
            continue
        rows = parse_earnings_history(payload, symbol)
        if not rows:
            empty.append(symbol)
            continue
        stored += store_surprises(session, iid, rows, fetched_at=now,
                                  source=source)
        fetched.append(symbol)
    return EarningsHistoryIngest(fetched=tuple(fetched), stored=stored,
                                 empty=tuple(empty), failed=tuple(failed))


def ingest_with_audit(session: Session, adapter: MarketDataAdapter,
                      symbols: list[str], *, clock: Clock,
                      failures: list[str]) -> EarningsHistoryIngest:
    """Ingest + emit the append-only audit event with counts (CLAUDE.md
    invariant 4). Shared by the CLI and the tests; fetched_at and the event's
    created_at both come from the injected clock."""
    now = clock.now()
    report = ingest_earnings_history(session, adapter, symbols, now=now,
                                     failures=failures)
    coverage = session.execute(text(
        "SELECT count(DISTINCT instrument_id) AS instruments, count(*) AS rows "
        "FROM market.earnings_surprises")).mappings().one()
    PostgresAuditLog(session, clock).append(
        event_type="market.earnings_history_ingest.completed",
        entity_type="market", entity_id=now.astimezone(UTC).date().isoformat(),
        actor_type="human", actor_id="earnings_history",
        payload={"now": now.isoformat(), "symbols": len(symbols),
                 "fetched": list(report.fetched), "empty": list(report.empty),
                 "failed": list(report.failed), "rows_stored": report.stored,
                 "failures": list(failures),
                 "coverage": {"instruments": int(coverage["instruments"]),
                              "rows": int(coverage["rows"])}})
    return report


def _resolve_symbols(session: Session, explicit: list[str] | None,
                     members: bool) -> list[str]:
    if explicit:
        return explicit
    if members:
        from atlas.dcp.market_data.index_membership import (INDEX_CODE,
                                                            load_membership)
        return sorted(r.ticker for r in load_membership(session,
                                                         index_code=INDEX_CODE))
    # default: every instrument that already holds vendor bars (the panel set)
    return [r.symbol for r in session.execute(text(
        "SELECT DISTINCT i.symbol FROM market.instruments i "
        "JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
        "WHERE pb.source = 'EodhdAdapter' ORDER BY i.symbol"))]


def main() -> None:
    """Operator run against the configured database: fetch Earnings::History
    for a symbol list and append completed surprises, with an audit event and
    a coverage summary. Exit 2 on any per-instrument vendor failure."""
    from atlas.core.clock import FrozenClock, SystemClock
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters import adapter_from_settings
    from atlas.dcp.market_data.index_membership import MEMBER_SEEDS

    p = argparse.ArgumentParser(
        description="Ingest EODHD Earnings::History into market.earnings_surprises "
                    "(append-only immutable facts)")
    p.add_argument("--symbols", help="comma-separated canonical symbols; "
                                     "default = every symbol with stored bars")
    p.add_argument("--members", action="store_true",
                   help="ingest all point-in-time index-membership tickers")
    p.add_argument("--now", help="aware ISO datetime pinning the clock for "
                                 "deterministic re-runs")
    a = p.parse_args()
    clock: Clock = (FrozenClock(datetime.fromisoformat(a.now)) if a.now
                    else SystemClock())
    explicit = ([s.strip() for s in a.symbols.split(",") if s.strip()]
                if a.symbols else None)
    adapter = adapter_from_settings(
        fixtures_root=ROOT / "tests" / "fixtures",
        seeds_csv=ROOT / "seeds" / "instruments_seed.csv",
        extra_seeds_csv=MEMBER_SEEDS if MEMBER_SEEDS.exists() else None)

    failures: list[str] = []
    with session_scope() as s:
        symbols = _resolve_symbols(s, explicit, a.members)
        report = ingest_with_audit(s, adapter, symbols, clock=clock,
                                   failures=failures)
        total = s.execute(text(
            "SELECT count(*) FROM market.earnings_surprises")).scalar()
    print(f"earnings_history: {len(report.fetched)} fetched "
          f"({report.stored} new rows), {len(report.empty)} empty, "
          f"{len(report.failed)} failed; {total} surprise rows on record")
    for msg in failures:
        print(f"FAILURE: {msg}")
    raise SystemExit(2 if failures else 0)


if __name__ == "__main__":
    main()
