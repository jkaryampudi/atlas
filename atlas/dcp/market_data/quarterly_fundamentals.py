"""Financials quarterly ingest -> market.quarterly_fundamentals (immutable FACTS).

The EODHD fundamentals document carries ``Financials.Income_Statement.quarterly``
and ``Financials.Balance_Sheet.quarterly`` blocks: one entry per
fiscal-period-end (the dict key, an ISO date), each with ``date`` (== the key),
``filing_date`` (when the figures became PUBLIC — the knowability anchor),
``currency_symbol`` and the statement lines. We read ONLY ``grossProfit`` and
``totalRevenue`` from the income statement and ``totalAssets`` from the balance
sheet (shape probed live 2026-07 against AVGO, AAPL and the
acquired-then-delisted ATVI — the latter carries 125 quarters through its 2023
acquisition with filing_date and totalAssets intact, so a survivorship-free
fundamentals panel is feasible). Values arrive as decimal STRINGS
('14919000000.00') and go through the same typed choke point as earnings.

DATA-CONVENTION FINDINGS (probed on real data, never assumed — the PEAD lesson):
  * Figures are AS-REPORTED, not restated, in every probe: AAPL 2018-12-31
    matches its original 10-Q exactly (grossProfit 32,031M, totalRevenue
    84,310M, costOfRevenue 52,279M; balance-sheet totalAssets 373,719M;
    filing_date 2019-01-30 = the actual 10-Q filing date). The vendor keeps ONE
    figure per quarter, so a LATER restatement would silently overwrite it —
    a rare, documented limitation (see migration 0026), not silently ignored.
  * ``filing_date`` is present on every probed quarter BUT is DEGENERATE
    (== fiscal_period_end, a physically impossible filing day) on a large
    minority: AVGO 46/78 income quarters (ALL of 2012-2017 plus 2022-07-31),
    AAPL 34/163 (pre-1994), ATVI 14/125. Trusting it would inject weeks of
    look-ahead, so such quarters are dropped FAIL-CLOSED here and counted
    (``degenerate_filing`` below). Income and balance statements of the same
    quarter carried identical filing_dates in every probe (0 disagreements in
    361 quarters); the merge below still takes the MAX of the two defensively.
  * Quarter key sets differ between statements (AVGO: 78 income vs 73 balance
    quarters), so the merge is by fiscal_period_end with NULLs where a
    statement is absent. Missing metrics stay missing — grossProfit is NEVER
    derived from totalRevenue minus a cost line (fail-closed).
  * RELEASE-STAGE ROWS ARE INCOMPLETE AND FREEZE THAT WAY: the vendor posts a
    quarter at the earnings release (JPM's 2026-06-30 row appeared with
    filing_date 2026-07-14 — the release, before any 10-Q — with metrics
    populated but currency_symbol NULL). Because this store is append-only, a
    quarter first fetched at release stage keeps its release-stage values
    forever; the vendor's later enrichment is never re-read. The SIGNAL
    therefore treats a NULL currency as unknown-not-mismatched
    (signals/quality/v1.py); sparse release-stage METRICS simply blank the
    name under its no-fallback rule — an honest, documented limitation.

WHAT IS STORED — anchorable quarters only. A row is stored iff its merged
filing_date is STRICTLY AFTER the fiscal period end AND at least one of
grossProfit / totalRevenue / totalAssets is a finite number. Rows with no
usable filing_date, a degenerate one, or no metrics are skipped and counted.

APPEND-ONLY (immutability). A filed statement is a settled historical fact.
Ingestion is ON CONFLICT (instrument_id, fiscal_period_end) DO NOTHING: a
stored row is never overwritten and re-ingestion is idempotent. Mirrors
market.earnings_surprises.

FAIL-SOFT PER INSTRUMENT. A vendor fetch failure (delisted 404, transport
error) or a missing instrument row is recorded in ``failures`` and reported;
the run continues. Honest coverage counts are the deliverable.

``fetched_at`` comes from the injected clock (CLAUDE.md invariant 6).

Usage:
  python -m atlas.dcp.market_data.quarterly_fundamentals --symbols AVGO,AAPL,ATVI
  python -m atlas.dcp.market_data.quarterly_fundamentals --members  # all PIT members
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

ROOT = Path(__file__).resolve().parents[3]
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class QuarterlyFundamentals:
    """One anchorable quarter — a settled fact. Metric values are the vendor's
    (as-reported in every live probe; see the module header for the restatement
    caveat). ``filing_date`` is strictly after ``fiscal_period_end`` by
    construction — the ingest refuses degenerate vendor rows."""
    symbol: str
    fiscal_period_end: date          # vendor quarterly key (period end)
    filing_date: date                # knowability anchor; > fiscal_period_end
    gross_profit: Decimal | None     # missing is missing (never derived)
    total_revenue: Decimal | None
    total_assets: Decimal | None
    currency: str | None = None


@dataclass(frozen=True)
class ParsedFundamentals:
    """Parse output with the honesty counts the audit event carries."""
    rows: tuple[QuarterlyFundamentals, ...]
    degenerate_filing: int   # filing_date <= period end (probed vendor defect)
    unanchorable: int        # no parseable filing_date at all
    metricless: int          # anchorable but every metric absent


def _decimal(value: object) -> Decimal | None:
    """A finite number (int/float/Decimal, or a plain-decimal string) -> Decimal,
    else None. Rejects bool, None, NaN/inf and free text. Statement lines may
    legitimately be negative (an operating-loss grossProfit) or zero, so no
    sign constraint here; the SIGNAL pins its own total_assets > 0 rule."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _currency(value: object) -> str | None:
    """ISO-4217-shaped currency code, else None."""
    return value if isinstance(value, str) and _CURRENCY_CODE.fullmatch(value) else None


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _quarterly_block(payload: dict[str, object], statement: str) -> dict[str, object]:
    fin = payload.get("Financials")
    stmt = fin.get(statement) if isinstance(fin, dict) else None
    q = stmt.get("quarterly") if isinstance(stmt, dict) else None
    return q if isinstance(q, dict) else {}


def parse_quarterly_fundamentals(payload: dict[str, object], symbol: str,
                                 ) -> ParsedFundamentals:
    """Vendor fundamentals document -> anchorable merged quarter rows.

    Reads ONLY Financials.Income_Statement.quarterly (grossProfit,
    totalRevenue) and Financials.Balance_Sheet.quarterly (totalAssets), merged
    by fiscal-period-end key; every field passes a typed choke point. The
    merged filing_date is the MAX over the statements present (the last of the
    figures to become public governs knowability; identical in every probe)
    and must be STRICTLY AFTER the period end, else the quarter cannot be
    point-in-time anchored and is dropped fail-closed (counted). Rows sorted
    by fiscal_period_end. A payload with no usable quarterly blocks yields no
    rows (a valid answer — ETFs have none)."""
    income = _quarterly_block(payload, "Income_Statement")
    balance = _quarterly_block(payload, "Balance_Sheet")
    rows: list[QuarterlyFundamentals] = []
    degenerate = unanchorable = metricless = 0
    for key in set(income) | set(balance):
        fpe = _date(key)
        if fpe is None:
            continue
        irow = income.get(key)
        brow = balance.get(key)
        irow = irow if isinstance(irow, dict) else {}
        brow = brow if isinstance(brow, dict) else {}
        filings = [d for d in (_date(irow.get("filing_date")),
                               _date(brow.get("filing_date"))) if d is not None]
        if not filings:
            unanchorable += 1
            continue
        filing = max(filings)
        if filing <= fpe:
            degenerate += 1        # probed vendor defect: impossible filing day
            continue
        gp = _decimal(irow.get("grossProfit"))
        tr = _decimal(irow.get("totalRevenue"))
        ta = _decimal(brow.get("totalAssets"))
        if gp is None and tr is None and ta is None:
            metricless += 1
            continue
        rows.append(QuarterlyFundamentals(
            symbol=symbol, fiscal_period_end=fpe, filing_date=filing,
            gross_profit=gp, total_revenue=tr, total_assets=ta,
            currency=(_currency(irow.get("currency_symbol"))
                      or _currency(brow.get("currency_symbol")))))
    rows.sort(key=lambda r: r.fiscal_period_end)
    return ParsedFundamentals(rows=tuple(rows), degenerate_filing=degenerate,
                              unanchorable=unanchorable, metricless=metricless)


def store_quarterly_fundamentals(session: Session, instrument_id: object,
                                 rows: tuple[QuarterlyFundamentals, ...], *,
                                 fetched_at: datetime, source: str) -> int:
    """Append-only insert of anchorable quarters. ON CONFLICT DO NOTHING on the
    natural key (instrument_id, fiscal_period_end): a settled fact is never
    overwritten and re-ingestion is idempotent. Returns rows newly inserted."""
    inserted = 0
    for r in rows:
        res = session.execute(text(
            "INSERT INTO market.quarterly_fundamentals "
            "(instrument_id, fiscal_period_end, filing_date, gross_profit, "
            " total_revenue, total_assets, currency, source, fetched_at) "
            "VALUES (:iid, :fpe, :fd, :gp, :tr, :ta, :cur, :src, :fa) "
            "ON CONFLICT (instrument_id, fiscal_period_end) DO NOTHING "
            "RETURNING id"),
            {"iid": instrument_id, "fpe": r.fiscal_period_end,
             "fd": r.filing_date, "gp": r.gross_profit, "tr": r.total_revenue,
             "ta": r.total_assets, "cur": r.currency, "src": source,
             "fa": fetched_at})
        inserted += 1 if res.first() is not None else 0
    return inserted


@dataclass(frozen=True)
class QuarterlyFundamentalsIngest:
    fetched: tuple[str, ...]     # vendor doc parsed and rows stored/idempotent
    stored: int                  # rows newly inserted this run (append-only)
    empty: tuple[str, ...]       # no anchorable quarters in the vendor doc
    failed: tuple[str, ...]      # vendor fetch failed / no instrument row
    degenerate_filing: int       # quarters dropped: filing_date <= period end
    unanchorable: int            # quarters dropped: no filing_date at all
    metricless: int              # quarters dropped: no usable metric


def ingest_quarterly_fundamentals(session: Session, adapter: MarketDataAdapter,
                                  symbols: list[str], *, now: datetime,
                                  failures: list[str],
                                  ) -> QuarterlyFundamentalsIngest:
    """Fetch + store anchorable quarterly fundamentals for each symbol.
    Fail-soft per instrument: a missing instrument row or a vendor failure is
    recorded in ``failures`` (alertable, exit 2 upstream) and the run
    continues."""
    source = type(adapter).__name__
    fetched: list[str] = []
    empty: list[str] = []
    failed: list[str] = []
    stored = degenerate = unanchorable = metricless = 0
    for symbol in symbols:
        iid = session.execute(text(
            "SELECT id FROM market.instruments WHERE symbol = :s"),
            {"s": symbol}).scalar()
        if iid is None:
            failures.append(f"quarterly_fundamentals {symbol}: no instrument row")
            failed.append(symbol)
            continue
        try:
            payload = adapter.fetch_fundamentals(symbol)
        except Exception as exc:  # vendor failure: recorded, not fatal
            failures.append(
                f"quarterly_fundamentals {symbol}: vendor fetch failed: {exc}")
            failed.append(symbol)
            continue
        parsed = parse_quarterly_fundamentals(payload, symbol)
        degenerate += parsed.degenerate_filing
        unanchorable += parsed.unanchorable
        metricless += parsed.metricless
        if not parsed.rows:
            empty.append(symbol)
            continue
        stored += store_quarterly_fundamentals(session, iid, parsed.rows,
                                               fetched_at=now, source=source)
        fetched.append(symbol)
    return QuarterlyFundamentalsIngest(
        fetched=tuple(fetched), stored=stored, empty=tuple(empty),
        failed=tuple(failed), degenerate_filing=degenerate,
        unanchorable=unanchorable, metricless=metricless)


def ingest_with_audit(session: Session, adapter: MarketDataAdapter,
                      symbols: list[str], *, clock: Clock,
                      failures: list[str]) -> QuarterlyFundamentalsIngest:
    """Ingest + emit the append-only audit event with counts (CLAUDE.md
    invariant 4). Shared by the CLI and the tests; fetched_at and the event's
    created_at both come from the injected clock."""
    now = clock.now()
    report = ingest_quarterly_fundamentals(session, adapter, symbols, now=now,
                                           failures=failures)
    coverage = session.execute(text(
        "SELECT count(DISTINCT instrument_id) AS instruments, count(*) AS rows "
        "FROM market.quarterly_fundamentals")).mappings().one()
    PostgresAuditLog(session, clock).append(
        event_type="market.quarterly_fundamentals_ingest.completed",
        entity_type="market", entity_id=now.astimezone(UTC).date().isoformat(),
        actor_type="human", actor_id="quarterly_fundamentals",
        payload={"now": now.isoformat(), "symbols": len(symbols),
                 "fetched": list(report.fetched), "empty": list(report.empty),
                 "failed": list(report.failed), "rows_stored": report.stored,
                 "dropped_degenerate_filing": report.degenerate_filing,
                 "dropped_unanchorable": report.unanchorable,
                 "dropped_metricless": report.metricless,
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
    """Operator run against the configured database: fetch quarterly financial
    statements for a symbol list and append anchorable quarters, with an audit
    event and a coverage summary. Exit 2 on any per-instrument vendor failure."""
    from atlas.core.clock import FrozenClock, SystemClock
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters import adapter_from_settings
    from atlas.dcp.market_data.index_membership import MEMBER_SEEDS

    p = argparse.ArgumentParser(
        description="Ingest EODHD quarterly Income_Statement/Balance_Sheet lines "
                    "into market.quarterly_fundamentals (append-only immutable "
                    "facts)")
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
            "SELECT count(*) FROM market.quarterly_fundamentals")).scalar()
    print(f"quarterly_fundamentals: {len(report.fetched)} fetched "
          f"({report.stored} new rows), {len(report.empty)} empty, "
          f"{len(report.failed)} failed; dropped "
          f"{report.degenerate_filing} degenerate-filing / "
          f"{report.unanchorable} unanchorable / "
          f"{report.metricless} metricless quarters; "
          f"{total} fundamental rows on record")
    for msg in failures:
        print(f"FAILURE: {msg}")
    raise SystemExit(2 if failures else 0)


if __name__ == "__main__":
    main()
