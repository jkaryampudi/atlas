"""Deterministic replay harness: run a full ingestion day from fixtures.

Usage: python -m atlas.dcp.market_data.replay --date 2024-07-15
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, date
from pathlib import Path

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.core.db import session_scope
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.ingest import ingest_day, seed_instruments

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
SEEDS = Path(__file__).resolve().parents[3] / "seeds"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    day = date.fromisoformat(p.parse_args().date)
    clock = FrozenClock(datetime(day.year, day.month, day.day, 22, 0, tzinfo=UTC))
    with session_scope() as s:
        seed_instruments(s, SEEDS / "instruments_seed.csv")
        audit = PostgresAuditLog(s, clock)
        status = ingest_day(session=s, adapter=FixtureAdapter(FIXTURES), audit=audit,
                            market="US", day=day, lookback_sessions=1)
        verified = audit.verify()
    print(f"replay {day}: gate={status.value} chain_verified={verified} events")


if __name__ == "__main__":
    main()
