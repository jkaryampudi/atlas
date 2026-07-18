"""Monthly external-source pick ingest (investing.com etc.) — step 1 of the
"learn which picks underperform and filter toward outperformers" plan.

For each ticker in a monthly list: ensure its data is present (the analyze
path's data prep — bars + one fundamentals snapshot, US-only v1), then record a
research.source_picks row with a POINT-IN-TIME feature snapshot anchored at the
last stored session on/before the recommendation date. Idempotent per
(source, ticker, recommendation_date).

This is the MEASUREMENT step: it captures the feature substrate (unrecoverable
later) and lets grade_picks/source_edge_report answer, after a few months,
"does this source's picks beat a dartboard". It deliberately does NOT create
proposals or committee memos (invariant 2). `--run-desk` optionally runs the
real evidence-grounded committee on each pick for a source-tagged memo (Atlas's
own view), under the analyze budget surface — off by default so the core
monthly ingest is free of model spend.

Usage:
  python -m atlas.ops.ingest_picks --source investing.com --date 2026-07-18 \
      --tickers AAPL,MSFT,NVDA
  python -m atlas.ops.ingest_picks --source investing.com --date 2026-07-18 \
      --file picks.txt            # one ticker per line
  python -m atlas.ops.ingest_picks --grade      # grade matured picks + report
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock, SystemClock
from atlas.core.db import session_scope
from atlas.dcp.research.source_picks import (
    grade_picks,
    record_pick,
    source_edge_report,
)
from atlas.ops.analyze import _build_adapter, _prepare_data, _resolve_instrument


@dataclass(frozen=True)
class PickResult:
    ticker: str
    outcome: str          # 'recorded' | 'duplicate' | 'no-data' | 'desk:<x>'
    detail: str


def _as_of_session(session: Session, instrument_id: str, on) -> object:
    """The last stored vendor bar_date on/before the recommendation date — the
    PIT anchor for the feature snapshot (features must use only what was
    knowable then). None when the instrument has no bar by that date."""
    return session.execute(text(
        "SELECT max(bar_date) FROM market.price_bars_daily "
        "WHERE instrument_id = :iid AND bar_date <= :on"),
        {"iid": instrument_id, "on": on}).scalar()


def ingest_picks(session: Session, clock: Clock, *, source: str,
                 recommendation_date, tickers: list[str],
                 run_desk: bool = False) -> list[PickResult]:
    """Record each pick with its PIT feature snapshot; optionally run the desk.
    One transaction owns the batch (the caller's session_scope commits it).
    Data prep per ticker mirrors the analyze path exactly."""
    results: list[PickResult] = []
    now = datetime.now(UTC)
    for raw in tickers:
        ticker = raw.strip().upper()
        if not ticker:
            continue
        iid, exchange, market, _known = _resolve_instrument(session, ticker)
        adapter = _build_adapter(ticker, exchange)
        try:
            prep = _prepare_data(session, adapter, ticker, iid, market, now)
        except Exception as e:  # noqa: BLE001 — no data is an honest per-ticker skip
            results.append(PickResult(ticker, "no-data", str(e)[:100]))
            continue
        as_of = _as_of_session(session, iid, recommendation_date)
        if as_of is None:
            results.append(PickResult(ticker, "no-data",
                                      f"no bar on/before {recommendation_date}"))
            continue
        pick_id = record_pick(session, source=source, ticker=ticker,
                              instrument_id=iid, recommendation_date=recommendation_date,
                              as_of_session=as_of)
        if pick_id is None:
            results.append(PickResult(ticker, "duplicate",
                                      f"already recorded for {source} {recommendation_date}"))
            continue
        detail = f"as_of {as_of}; {prep}"
        if run_desk:
            detail += "; " + _run_desk_for(session, ticker, source)
        results.append(PickResult(ticker, "recorded", detail))
    return results


def _run_desk_for(session: Session, ticker: str, source: str) -> str:
    """Optional committee enrichment: a real, evidence-grounded, source-tagged
    memo (Atlas's own view of the pick), under the analyze budget surface.
    Deferred import keeps the DCP-only core free of the agents package."""
    from atlas.agents.desk import run_desk
    from atlas.agents.runtime.runner import budget_surface
    try:
        with budget_surface("analyze"):
            report = run_desk(session, SystemClock(), [ticker], source=source)
    except Exception as e:  # noqa: BLE001 — budget/desk failure never fails the record
        return f"desk error: {str(e)[:80]}"
    if report.memos:
        m = report.memos[0]
        return f"desk: {m.recommendation} ({m.conviction})"
    if report.cage_holds:
        return "desk: CAGE HELD"
    return "desk: no memo"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest a monthly external-source pick list")
    p.add_argument("--source", help="e.g. investing.com")
    p.add_argument("--date", help="recommendation date YYYY-MM-DD (default: today UTC)")
    p.add_argument("--tickers", help="comma-separated ticker list")
    p.add_argument("--file", help="file with one ticker per line")
    p.add_argument("--run-desk", action="store_true",
                   help="also run the committee on each pick (analyze budget surface)")
    p.add_argument("--grade", action="store_true",
                   help="grade matured picks and print the per-source edge report")
    a = p.parse_args(argv)
    clock = SystemClock()

    if a.grade:
        with session_scope() as s:
            g = grade_picks(s, clock)
            print(f"graded {g.graded} outcome(s); {g.still_immature} still immature")
            print("\nsource edge (outperform-rate vs dartboard; near-zero edge = no skill):")
            for e in source_edge_report(s):
                if e.n_matured == 0:
                    print(f"  {e.source:16s} h{e.horizon}: no matured picks yet")
                else:
                    print(f"  {e.source:16s} h{e.horizon}: n={e.n_matured} "
                          f"outperform={e.outperform_rate:.1%} "
                          f"dartboard={e.dartboard:.1%} EDGE={e.edge:+.1%}")
        return 0

    if not a.source:
        p.error("--source is required (unless --grade)")
    rec_date = (datetime.strptime(a.date, "%Y-%m-%d").date() if a.date
                else datetime.now(UTC).date())
    tickers: list[str] = []
    if a.tickers:
        tickers += a.tickers.split(",")
    if a.file:
        tickers += Path(a.file).read_text().splitlines()
    if not tickers:
        p.error("provide --tickers or --file")

    with session_scope() as s:
        results = ingest_picks(s, clock, source=a.source, recommendation_date=rec_date,
                               tickers=tickers, run_desk=a.run_desk)
    rec = sum(1 for r in results if r.outcome == "recorded")
    dup = sum(1 for r in results if r.outcome == "duplicate")
    nod = sum(1 for r in results if r.outcome == "no-data")
    print(f"{a.source} {rec_date}: recorded {rec}, duplicate {dup}, no-data {nod}")
    for r in results:
        print(f"  {r.ticker:8s} {r.outcome:10s} {r.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
