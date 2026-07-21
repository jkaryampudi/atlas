"""Deterministic replay harness: run a full ingestion day from fixtures.

Usage: python -m atlas.dcp.market_data.replay --date 2024-07-15
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.core.db import session_scope
from atlas.core.workflow import Node, WorkflowRunner
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.ingest import ingest_day, seed_instruments

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
SEEDS = Path(__file__).resolve().parents[3] / "seeds"


def assert_disposable_db(session: Session, *, force: bool) -> None:
    """F-017: replay SEEDS fixture instruments and upserts FIXTURE bars via
    ON CONFLICT DO UPDATE — running it against the production `atlas` database
    silently overwrites real EODHD market data. Refuse any target database whose
    name is not clearly disposable (ends in `_test` or contains `replay`) unless
    the operator passes --force. Fail closed."""
    name = str(session.execute(text("SELECT current_database()")).scalar_one())
    if force:
        return
    if not (name.endswith("_test") or name.startswith("atlas_test") or "replay" in name):
        raise SystemExit(
            f"replay refuses to write fixtures into database {name!r}: it seeds "
            "instruments and upserts fixture bars (ON CONFLICT DO UPDATE), which "
            "would overwrite real market data. Point ATLAS_DATABASE_URL at a "
            "disposable database whose name ends in '_test' or contains 'replay', "
            "or pass --force to override deliberately.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    p.add_argument("--force", action="store_true",
                   help="override the disposable-database guard (F-017)")
    args = p.parse_args()
    day = date.fromisoformat(args.date)
    clock = FrozenClock(datetime(day.year, day.month, day.day, 22, 0, tzinfo=UTC))
    with session_scope() as s:
        assert_disposable_db(s, force=args.force)
        audit = PostgresAuditLog(s, clock)
        # checkpointed daily cycle (ADR-0005 pattern 3): re-running the same
        # date skips completed nodes instead of re-ingesting
        runner = WorkflowRunner(s, audit, clock)
        results = runner.run(f"replay-{day}", [
            Node("seed_instruments",
                 lambda: str(seed_instruments(s, SEEDS / "instruments_seed.csv"))),
            Node("ingest_day",
                 lambda: ingest_day(session=s, adapter=FixtureAdapter(FIXTURES),
                                    audit=audit, market="US", day=day,
                                    lookback_sessions=1).value),
        ])
        verified = audit.verify()
    print(f"replay {day}: gate={results['ingest_day']} chain_verified={verified} events")


if __name__ == "__main__":
    main()
