"""Earnings::Trend ingest -> market.estimate_snapshots (append-only PIT archive).

THE FORWARD ARCHIVE (ADR-0011). EODHD's ``Earnings.Trend`` block is a CURRENT
snapshot the vendor overwrites in place — there is no vendor history of what
the consensus looked like yesterday (established 2026-07-15; PEAD was chosen
as the backtestable cousin for exactly this reason). This module records the
consensus OURSELVES, daily from today forward, so that in months we hold true
point-in-time revisions history: one row per (instrument, fiscal_period_end,
snapshot_date) preserving what the vendor said on that session. Every day not
recorded is lost forever. Rows are immutable facts: ON CONFLICT DO NOTHING —
tomorrow's differing value is a NEW row, never an update (migration 0028).

Shape (probed live 2026-07-15, re-verified 2026-07-17 against AAPL):
``fundamentals/{code}`` -> ``Earnings.Trend`` is a dict keyed by fiscal-period
-end ISO date; values arrive as decimal STRINGS ('1.9404') with genuine nulls
(epsRevisionsDownLast7days beside populated up-legs), through the same typed
choke point as earnings_history. Archived fields: earningsEstimateAvg,
earningsEstimateNumberOfAnalysts, revenueEstimateAvg, epsTrendCurrent/
7daysAgo/30daysAgo, epsRevisionsUp/DownLast7/30days.

NEAR-PERIOD WINDOW (structural, not searched). The vendor keeps stale periods
back to 2017 in the same block; archiving them daily would bloat the table
with dead consensus nobody revises. Only fiscal periods ending within
[today - PAST_WINDOW_DAYS, today + FUTURE_WINDOW_DAYS] are stored — the
actionable consensus horizon: 120 days back covers a just-ended quarter whose
estimate is still live pre-report; 400 days forward covers the current + next
fiscal year rows analysts actively revise. These bounds shape which rows the
archive keeps; they were never tuned against outcomes and are not parameters
of any signal.

ONCE-DAILY GUARD. If ANY instrument already has a row for the injected clock's
UTC session, the run skips idempotently (counts say so) — the nightly cycle
must not double-snapshot on re-runs/replays, and a same-session second fetch
would be pure vendor spend for guaranteed ON-CONFLICT no-ops. The guard is
per-session, not per-instrument: a partial first run (some instruments failed)
is NOT silently completed by a re-run — the failure counts are the honest
record, and the next session resumes everyone.

FAIL-SOFT PER INSTRUMENT: a vendor failure or missing instrument row is
recorded in ``failures`` (alertable, exit 2 upstream) and the run continues.
``fetched_at``/``snapshot_date`` come from the injected clock (invariant 6).

BOUNDARY — RESEARCH-ONLY, AND NOT EVEN THAT YET. This module feeds NO signal,
NO evidence block, and NO desk integration. latest_snapshot/snapshot_series
exist for future research reads only; a revisions factor spec is not worth
writing before roughly six months of daily snapshots have accrued, and any
factor built on this archive goes through the unmodified gauntlet (ADR-0011).

Usage:
  python -m atlas.dcp.market_data.estimate_snapshots --universe --once-daily
  python -m atlas.dcp.market_data.estimate_snapshots --symbols AVGO,MSFT
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.market_data.adapters.base import MarketDataAdapter

ROOT = Path(__file__).resolve().parents[3]

# Near-period window bounds (module header: structural, never tuned/searched).
PAST_WINDOW_DAYS = 120
FUTURE_WINDOW_DAYS = 400


@dataclass(frozen=True)
class EstimatePeriod:
    """One fiscal period's consensus as the vendor showed it at fetch time.
    Every leg is optional: NULL records the vendor's genuine absence."""
    fiscal_period_end: date
    eps_estimate_avg: Decimal | None
    eps_estimate_analysts: Decimal | None
    revenue_estimate_avg: Decimal | None
    eps_trend_current: Decimal | None
    eps_trend_7d: Decimal | None
    eps_trend_30d: Decimal | None
    revisions_up_7d: Decimal | None
    revisions_up_30d: Decimal | None
    revisions_down_7d: Decimal | None
    revisions_down_30d: Decimal | None


def _decimal(value: object) -> Decimal | None:
    """A finite number (int/float/Decimal, or a plain-decimal string) ->
    Decimal, else None. Rejects bool, None, NaN/inf and free text — the same
    typed choke point as earnings_history/quarterly_fundamentals."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def parse_estimate_trend(payload: dict[str, object], *,
                         today: date) -> list[EstimatePeriod]:
    """Vendor fundamentals document -> near-period consensus rows.

    Reads ONLY Earnings.Trend; keeps fiscal periods inside the structural
    near-period window; drops malformed keys and periods whose every archived
    leg is null (nothing to preserve). Returns rows sorted by
    fiscal_period_end. No usable Trend block yields an empty list (a valid
    answer — ETFs and uncovered names have none)."""
    earnings = payload.get("Earnings")
    trend = earnings.get("Trend") if isinstance(earnings, dict) else None
    if not isinstance(trend, dict):
        return []
    lo = today - timedelta(days=PAST_WINDOW_DAYS)
    hi = today + timedelta(days=FUTURE_WINDOW_DAYS)
    out: list[EstimatePeriod] = []
    for key, row in trend.items():
        if not isinstance(row, dict):
            continue
        try:
            fpe = date.fromisoformat(str(key))
        except ValueError:
            continue
        if not lo <= fpe <= hi:
            continue  # stale/far period: outside the actionable horizon
        period = EstimatePeriod(
            fiscal_period_end=fpe,
            eps_estimate_avg=_decimal(row.get("earningsEstimateAvg")),
            eps_estimate_analysts=_decimal(row.get("earningsEstimateNumberOfAnalysts")),
            revenue_estimate_avg=_decimal(row.get("revenueEstimateAvg")),
            eps_trend_current=_decimal(row.get("epsTrendCurrent")),
            eps_trend_7d=_decimal(row.get("epsTrend7daysAgo")),
            eps_trend_30d=_decimal(row.get("epsTrend30daysAgo")),
            revisions_up_7d=_decimal(row.get("epsRevisionsUpLast7days")),
            revisions_up_30d=_decimal(row.get("epsRevisionsUpLast30days")),
            revisions_down_7d=_decimal(row.get("epsRevisionsDownLast7days")),
            revisions_down_30d=_decimal(row.get("epsRevisionsDownLast30days")))
        if all(getattr(period, f) is None for f in (
                "eps_estimate_avg", "eps_estimate_analysts", "revenue_estimate_avg",
                "eps_trend_current", "eps_trend_7d", "eps_trend_30d",
                "revisions_up_7d", "revisions_up_30d", "revisions_down_7d",
                "revisions_down_30d")):
            continue  # all-null period: no consensus to preserve
        out.append(period)
    out.sort(key=lambda r: r.fiscal_period_end)
    return out


def store_snapshots(session: Session, instrument_id: object,
                    rows: list[EstimatePeriod], *, snapshot_date: date,
                    fetched_at: datetime, source: str) -> int:
    """Append-only insert. ON CONFLICT (instrument_id, fiscal_period_end,
    snapshot_date) DO NOTHING: a recorded snapshot is never overwritten and a
    re-run is idempotent; a changed vendor value lands as a NEW row under a
    NEW snapshot_date. Returns rows newly inserted."""
    inserted = 0
    for r in rows:
        res = session.execute(text(
            "INSERT INTO market.estimate_snapshots "
            "(instrument_id, fiscal_period_end, snapshot_date, "
            " eps_estimate_avg, eps_estimate_analysts, revenue_estimate_avg, "
            " eps_trend_current, eps_trend_7d, eps_trend_30d, "
            " revisions_up_7d, revisions_up_30d, revisions_down_7d, "
            " revisions_down_30d, source, fetched_at) "
            "VALUES (:iid, :fpe, :sd, :ea, :an, :rev, :tc, :t7, :t30, "
            "        :u7, :u30, :d7, :d30, :src, :fa) "
            "ON CONFLICT (instrument_id, fiscal_period_end, snapshot_date) "
            "DO NOTHING RETURNING id"),
            {"iid": instrument_id, "fpe": r.fiscal_period_end, "sd": snapshot_date,
             "ea": r.eps_estimate_avg, "an": r.eps_estimate_analysts,
             "rev": r.revenue_estimate_avg, "tc": r.eps_trend_current,
             "t7": r.eps_trend_7d, "t30": r.eps_trend_30d,
             "u7": r.revisions_up_7d, "u30": r.revisions_up_30d,
             "d7": r.revisions_down_7d, "d30": r.revisions_down_30d,
             "src": source, "fa": fetched_at})
        inserted += 1 if res.first() is not None else 0
    return inserted


def snapshot_taken(session: Session, snapshot_date: date) -> bool:
    """The once-daily guard's question: does ANY instrument already hold a
    snapshot row for this session?"""
    return session.execute(text(
        "SELECT 1 FROM market.estimate_snapshots WHERE snapshot_date = :d "
        "LIMIT 1"), {"d": snapshot_date}).scalar() is not None


@dataclass(frozen=True)
class EstimateSnapshotDaily:
    fetched: tuple[str, ...]  # vendor doc parsed and near-period rows stored
    stored: int               # rows newly inserted this run (append-only)
    empty: tuple[str, ...]    # no near-period Trend consensus in the vendor doc
    failed: tuple[str, ...]   # vendor fetch failed / no instrument row
    skipped: bool             # once-daily guard: this session already snapshot


def snapshot_estimates(session: Session, adapter: MarketDataAdapter,
                       symbols: list[str], *, now: datetime,
                       failures: list[str],
                       once_daily: bool = True) -> EstimateSnapshotDaily:
    """One consensus snapshot per symbol for the injected clock's UTC session.
    ``once_daily=True`` (the nightly default) makes a session that already
    holds any snapshot row an idempotent skip. Fail-soft per instrument: a
    missing instrument row or a vendor failure is recorded in ``failures``
    (alertable, exit 2 upstream) and the run continues."""
    source = type(adapter).__name__
    today = now.astimezone(UTC).date()
    if once_daily and snapshot_taken(session, today):
        return EstimateSnapshotDaily(fetched=(), stored=0, empty=(), failed=(),
                                     skipped=True)
    fetched: list[str] = []
    empty: list[str] = []
    failed: list[str] = []
    stored = 0
    for symbol in symbols:
        iid = session.execute(text(
            "SELECT id FROM market.instruments WHERE symbol = :s"),
            {"s": symbol}).scalar()
        if iid is None:
            failures.append(f"estimates {symbol}: no instrument row")
            failed.append(symbol)
            continue
        try:
            payload = adapter.fetch_fundamentals(symbol)
        except Exception as exc:  # vendor failure: recorded, not fatal
            failures.append(f"estimates {symbol}: vendor fetch failed: {exc}")
            failed.append(symbol)
            continue
        rows = parse_estimate_trend(payload, today=today)
        if not rows:
            empty.append(symbol)
            continue
        stored += store_snapshots(session, iid, rows, snapshot_date=today,
                                  fetched_at=now, source=source)
        fetched.append(symbol)
    return EstimateSnapshotDaily(fetched=tuple(fetched), stored=stored,
                                 empty=tuple(empty), failed=tuple(failed),
                                 skipped=False)


def snapshot_with_audit(session: Session, adapter: MarketDataAdapter,
                        symbols: list[str], *, clock: Clock,
                        failures: list[str],
                        once_daily: bool = True) -> EstimateSnapshotDaily:
    """Snapshot + emit the ONE append-only audit event with counts (CLAUDE.md
    invariant 4). Shared by the CLI and the tests; fetched_at and the event's
    created_at both come from the injected clock. The coverage block tracks
    the archive's accrual — sessions is the number every future factor spec
    will be judged against."""
    now = clock.now()
    report = snapshot_estimates(session, adapter, symbols, now=now,
                                failures=failures, once_daily=once_daily)
    coverage = session.execute(text(
        "SELECT count(DISTINCT instrument_id) AS instruments, "
        "       count(DISTINCT snapshot_date) AS sessions, count(*) AS rows "
        "FROM market.estimate_snapshots")).mappings().one()
    PostgresAuditLog(session, clock).append(
        event_type="market.estimate_snapshot_ingest.completed",
        entity_type="market", entity_id=now.astimezone(UTC).date().isoformat(),
        actor_type="human", actor_id="estimate_snapshots",
        payload={"now": now.isoformat(), "symbols": len(symbols),
                 "skipped": report.skipped, "fetched": list(report.fetched),
                 "empty": list(report.empty), "failed": list(report.failed),
                 "rows_stored": report.stored, "failures": list(failures),
                 "coverage": {"instruments": int(coverage["instruments"]),
                              "sessions": int(coverage["sessions"]),
                              "rows": int(coverage["rows"])}})
    return report


def universe_symbols(session: Session) -> list[str]:
    """The forward archive's population: ACTIVE US single names (stock/adr) —
    the ADR-0007 trading universe, matching the signal-generation universe
    rule (signals/pead/generate.py). This is a LIVE forward archive, not a
    backtest: point-in-time index membership is irrelevant here — what matters
    is recording the names the desk can actually trade, every day."""
    return [str(sym) for sym in session.execute(text(
        "SELECT symbol FROM market.instruments "
        "WHERE is_active AND market = 'US' "
        "  AND instrument_type IN ('stock','adr') ORDER BY symbol")).scalars()]


# --- read API (research-only; see the module-header boundary note) ----------

@dataclass(frozen=True)
class EstimateSnapshotRow:
    """One stored archive row, as recorded — no derivation, no adjustment."""
    symbol: str
    fiscal_period_end: date
    snapshot_date: date
    eps_estimate_avg: Decimal | None
    eps_estimate_analysts: Decimal | None
    revenue_estimate_avg: Decimal | None
    eps_trend_current: Decimal | None
    eps_trend_7d: Decimal | None
    eps_trend_30d: Decimal | None
    revisions_up_7d: Decimal | None
    revisions_up_30d: Decimal | None
    revisions_down_7d: Decimal | None
    revisions_down_30d: Decimal | None
    source: str
    fetched_at: datetime


_ROW_COLUMNS = ("es.fiscal_period_end, es.snapshot_date, es.eps_estimate_avg, "
                "es.eps_estimate_analysts, es.revenue_estimate_avg, "
                "es.eps_trend_current, es.eps_trend_7d, es.eps_trend_30d, "
                "es.revisions_up_7d, es.revisions_up_30d, es.revisions_down_7d, "
                "es.revisions_down_30d, es.source, es.fetched_at")


def _row(symbol: str, m: dict[str, object]) -> EstimateSnapshotRow:
    def dec(key: str) -> Decimal | None:
        v = m[key]
        assert v is None or isinstance(v, Decimal)
        return v

    fpe, sd, src, fa = (m["fiscal_period_end"], m["snapshot_date"],
                        m["source"], m["fetched_at"])
    assert (isinstance(fpe, date) and isinstance(sd, date)
            and isinstance(src, str) and isinstance(fa, datetime))
    return EstimateSnapshotRow(
        symbol=symbol, fiscal_period_end=fpe, snapshot_date=sd,
        eps_estimate_avg=dec("eps_estimate_avg"),
        eps_estimate_analysts=dec("eps_estimate_analysts"),
        revenue_estimate_avg=dec("revenue_estimate_avg"),
        eps_trend_current=dec("eps_trend_current"),
        eps_trend_7d=dec("eps_trend_7d"), eps_trend_30d=dec("eps_trend_30d"),
        revisions_up_7d=dec("revisions_up_7d"),
        revisions_up_30d=dec("revisions_up_30d"),
        revisions_down_7d=dec("revisions_down_7d"),
        revisions_down_30d=dec("revisions_down_30d"),
        source=src, fetched_at=fa)


def latest_snapshot(session: Session, symbol: str, *,
                    on: date) -> tuple[EstimateSnapshotRow, ...] | None:
    """The full consensus state (every archived fiscal period) from the most
    recent snapshot session at or before ``on`` — "what did the vendor show
    for this name as of date X". None when no snapshot at or before ``on``
    exists: absence is absence, never an empty fabrication."""
    snap = session.execute(text(
        "SELECT max(es.snapshot_date) FROM market.estimate_snapshots es "
        "JOIN market.instruments i ON i.id = es.instrument_id "
        "WHERE i.symbol = :sym AND es.snapshot_date <= :on"),
        {"sym": symbol, "on": on}).scalar()
    if snap is None:
        return None
    rows = session.execute(text(
        f"SELECT {_ROW_COLUMNS} FROM market.estimate_snapshots es "
        "JOIN market.instruments i ON i.id = es.instrument_id "
        "WHERE i.symbol = :sym AND es.snapshot_date = :sd "
        "ORDER BY es.fiscal_period_end"),
        {"sym": symbol, "sd": snap}).mappings().all()
    return tuple(_row(symbol, dict(m)) for m in rows)


def snapshot_series(session: Session, symbol: str, *, fiscal_period_end: date,
                    start: date | None = None,
                    end: date | None = None) -> tuple[EstimateSnapshotRow, ...]:
    """The day-by-day archive for ONE fiscal period — the true point-in-time
    revisions history this whole build exists to accrue. Ordered by
    snapshot_date; empty tuple when nothing is on record in the range."""
    rows = session.execute(text(
        f"SELECT {_ROW_COLUMNS} FROM market.estimate_snapshots es "
        "JOIN market.instruments i ON i.id = es.instrument_id "
        "WHERE i.symbol = :sym AND es.fiscal_period_end = :fpe "
        "  AND (CAST(:start AS date) IS NULL OR es.snapshot_date >= :start) "
        "  AND (CAST(:end AS date) IS NULL OR es.snapshot_date <= :end) "
        "ORDER BY es.snapshot_date"),
        {"sym": symbol, "fpe": fiscal_period_end, "start": start,
         "end": end}).mappings().all()
    return tuple(_row(symbol, dict(m)) for m in rows)


def main() -> None:
    """Operator run against the configured database: snapshot the consensus
    for a symbol list (or the ADR-0007 active US single-name universe), with
    the audit event and an accrual summary. Exit 2 on any per-instrument
    vendor failure."""
    from atlas.core.clock import FrozenClock, SystemClock
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters import adapter_from_settings

    p = argparse.ArgumentParser(
        description="Snapshot EODHD Earnings::Trend consensus into "
                    "market.estimate_snapshots (append-only point-in-time "
                    "forward archive — ADR-0011)")
    p.add_argument("--symbols", help="comma-separated canonical symbols")
    p.add_argument("--universe", action="store_true",
                   help="snapshot every ACTIVE US stock/adr (the ADR-0007 "
                        "trading universe)")
    p.add_argument("--once-daily", action="store_true", dest="once_daily",
                   help="skip idempotently when this session already holds "
                        "any snapshot row (the nightly/cron guard)")
    p.add_argument("--now", help="aware ISO datetime pinning the clock for "
                                 "deterministic re-runs")
    a = p.parse_args()
    if not a.symbols and not a.universe:
        p.error("pass --symbols or --universe (a deliberate choice, no default)")
    clock: Clock = (FrozenClock(datetime.fromisoformat(a.now)) if a.now
                    else SystemClock())
    adapter = adapter_from_settings(fixtures_root=ROOT / "tests" / "fixtures",
                                    seeds_csv=ROOT / "seeds" / "instruments_seed.csv")

    failures: list[str] = []
    with session_scope() as s:
        symbols = ([sym.strip() for sym in a.symbols.split(",") if sym.strip()]
                   if a.symbols else universe_symbols(s))
        report = snapshot_with_audit(s, adapter, symbols, clock=clock,
                                     failures=failures,
                                     once_daily=a.once_daily)
        total = s.execute(text(
            "SELECT count(DISTINCT snapshot_date) FROM market.estimate_snapshots"
        )).scalar()
    if report.skipped:
        print("estimates: session already snapshot — once-daily guard skipped "
              "the run (idempotent)")
    print(f"estimates: {len(report.fetched)} fetched ({report.stored} new rows), "
          f"{len(report.empty)} empty, {len(report.failed)} failed; "
          f"{total} snapshot session(s) accrued")
    for msg in failures:
        print(f"FAILURE: {msg}")
    raise SystemExit(2 if failures else 0)


if __name__ == "__main__":
    main()
