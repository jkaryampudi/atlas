"""Feature-store backfill CLI (ADR-0011 step 1).

    python -m atlas.dcp.features.backfill --feature momentum_12_1 \
        --from 2025-01-02 --to 2025-06-30 [--symbols AAPL,MSFT] [--now ISO]

Materializes a registered feature over US trading sessions in [--from, --to]
for the ADR-0007 trading universe (active US 'stock'/'adr' instruments — the
same set the production rankers score; ETFs are excluded by construction) or
an explicit --symbols list, reading only stored bars/earnings. Fail-soft per
symbol: one broken series is recorded and the run continues; ONE append-only
audit event carries the counts (CLAUDE.md invariant 4). Exit 2 when any
symbol failed — honest partial coverage is reported, never hidden.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.features.definitions import FEATURES, get_feature
from atlas.dcp.features.store import (
    FeatureDefinition,
    MaterializeReport,
    materialize,
)
from atlas.dcp.market_data.calendars import trading_days_between


def trading_universe(db: Session) -> list[str]:
    """The ADR-0007 trading universe the production rankers score: active US
    single names (stock/adr). SPY/QQQ/India-sleeve ETFs are 'etf' by type and
    excluded by construction."""
    return [str(r.symbol) for r in db.execute(text(
        "SELECT symbol FROM market.instruments "
        "WHERE is_active AND market = 'US' "
        "  AND instrument_type IN ('stock','adr') ORDER BY symbol"))]


def backfill_feature(db: Session, feature: FeatureDefinition, *, clock: Clock,
                     symbols: list[str], start: date,
                     end: date) -> MaterializeReport:
    """Materialize over every trading session of the feature's market in
    [start, end] and emit the audit event with counts. Shared by the CLI and
    the tests; every timestamp comes from the injected clock."""
    sessions = trading_days_between(feature.market, start, end)
    if not sessions:
        raise ValueError(f"no {feature.market} trading sessions in "
                         f"[{start}, {end}]")
    report = materialize(db, feature, clock=clock, symbols=symbols,
                         sessions=sessions)
    PostgresAuditLog(db, clock).append(
        event_type="quant.feature.materialized", entity_type="feature",
        entity_id=f"{feature.name}/{report.dataset_version[:12]}",
        actor_type="human", actor_id="feature_backfill",
        payload={"feature": feature.name, "version": feature.version,
                 "dataset_version": report.dataset_version,
                 "from": start.isoformat(), "to": end.isoformat(),
                 "sessions": report.sessions, "symbols": len(symbols),
                 "inserted": report.inserted, "existing": report.existing,
                 "computed": dict(report.computed),
                 "failed": list(report.failed),
                 "failures": list(report.failures)})
    return report


def main() -> None:
    from atlas.core.clock import FrozenClock, SystemClock
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(
        description="Materialize a registered feature into the point-in-time "
                    "feature store (append-only; new data => new "
                    "dataset_version)")
    p.add_argument("--feature", required=True, choices=sorted(FEATURES),
                   help="registered feature name")
    p.add_argument("--from", dest="start", required=True, type=date.fromisoformat,
                   help="first target session (ISO date, inclusive)")
    p.add_argument("--to", dest="end", required=True, type=date.fromisoformat,
                   help="last target session (ISO date, inclusive)")
    p.add_argument("--symbols", help="comma-separated symbols; default = the "
                                     "ADR-0007 trading universe")
    p.add_argument("--now", help="aware ISO datetime pinning the clock for "
                                 "deterministic re-runs")
    a = p.parse_args()
    if a.start > a.end:
        p.error(f"--from {a.start} is after --to {a.end}")
    clock: Clock = (FrozenClock(datetime.fromisoformat(a.now)) if a.now
                    else SystemClock())
    feature = get_feature(a.feature)
    explicit = ([s.strip() for s in a.symbols.split(",") if s.strip()]
                if a.symbols else None)

    with session_scope() as db:
        symbols = explicit if explicit is not None else trading_universe(db)
        if not symbols:
            p.error("no symbols to materialize (empty universe and no "
                    "--symbols)")
        report = backfill_feature(db, feature, clock=clock, symbols=symbols,
                                  start=a.start, end=a.end)
    print(f"feature {report.feature}: dataset_version "
          f"{report.dataset_version[:12]}… over {report.sessions} sessions — "
          f"{report.inserted} values inserted, {report.existing} already "
          f"present, {len(report.computed)} of {len(symbols)} symbols "
          f"computed, {len(report.failed)} failed")
    for msg in report.failures:
        print(f"FAILURE: {msg}")
    raise SystemExit(2 if report.failed else 0)


if __name__ == "__main__":
    main()
