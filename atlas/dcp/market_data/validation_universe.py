"""Validation-only instruments (survivorship cross-check, xsmom round 2).

seeds/validation_instruments.csv holds the NINE original Select Sector SPDR
ETFs (XLB XLE XLF XLI XLK XLP XLU XLV XLY) — trading continuously since 1998
with zero survivorship: sector funds are never deleted for losing, and the
sector set is fixed by construction (no discretion = no selection bias). They
exist ONLY to cross-check the ADR-0007 S&P-100 xsmom result, whose PASS is
conditional on survivorship (docs/reports/xsmom-momentum-2026-07.md).

They are seeded with **is_active = FALSE** and must stay invisible to every
tradable-universe surface — quality gates / inception (quality.inception_map),
the scanner, the desk, and the default xsmom panel all filter on is_active
(pinned by tests/integration/test_validation_universe_pg.py). The signed
tradable universe (seeds/universe.json, ADR-0007) is NOT touched: this CSV is
a separate, documented seeds mechanism, and sync_universe never reads it.

Fail-closed seeding rules:
- an entry whose SYMBOL already exists in market.instruments as an ACTIVE
  instrument (any exchange) is REFUSED loudly — a validation instrument must
  never collide with the tradable universe (symbol-keyed loaders would mix
  the two series);
- an entry already present and inactive is skipped (idempotent re-seed);
- seeding never activates, deactivates or updates an existing instrument.

Usage: python -m atlas.dcp.market_data.validation_universe
Then backfill bars deliberately via the additive symbols mode:
python -m atlas.dcp.market_data.backfill --symbols XLB,... --from ... --end ...
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[3]
VALIDATION_SEEDS = ROOT / "seeds" / "validation_instruments.csv"


@dataclass(frozen=True)
class ValidationSeedResult:
    inserted: tuple[str, ...]        # "SYMBOL@EXCHANGE", is_active=FALSE
    already_present: tuple[str, ...]  # existing inactive rows, left untouched


def seed_validation_instruments(session: Session,
                                csv_path: Path = VALIDATION_SEEDS) -> ValidationSeedResult:
    """Insert the validation-only instruments with is_active = FALSE.
    Refuses (ValueError) when a seeded symbol already exists as an ACTIVE
    instrument; idempotent over its own prior inserts; never updates rows."""
    inserted: list[str] = []
    already: list[str] = []
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            key = f"{row['symbol']}@{row['exchange']}"
            active = session.execute(text(
                "SELECT exchange FROM market.instruments "
                "WHERE symbol = :s AND is_active"), {"s": row["symbol"]}).scalar()
            if active is not None:
                raise ValueError(
                    f"validation instrument {row['symbol']!r} collides with an "
                    f"ACTIVE instrument ({row['symbol']}@{active}) — a validation "
                    "symbol must never overlap the tradable universe")
            exists = session.execute(text(
                "SELECT 1 FROM market.instruments "
                "WHERE symbol = :s AND exchange = :e"),
                {"s": row["symbol"], "e": row["exchange"]}).scalar()
            if exists:
                already.append(key)
                continue
            session.execute(text(
                "INSERT INTO market.instruments "
                "(symbol, exchange, market, instrument_type, name, sector_gics, "
                " currency, economic_exposure, is_active) "
                "VALUES (:symbol, :exchange, :market, :instrument_type, :name, "
                "        :sector_gics, :currency, "
                "        string_to_array(:economic_exposure, '|'), FALSE)"), row)
            inserted.append(key)
    return ValidationSeedResult(inserted=tuple(inserted), already_present=tuple(already))


def main() -> None:
    from atlas.core.audit_repo import PostgresAuditLog
    from atlas.core.clock import SystemClock
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(
        description="Seed validation-only instruments (is_active=FALSE; never "
                    "tradable; not part of the signed universe manifest)")
    p.add_argument("--path", type=Path, default=VALIDATION_SEEDS)
    a = p.parse_args()

    with session_scope() as s:
        res = seed_validation_instruments(s, a.path)
        PostgresAuditLog(s, SystemClock()).append(
            event_type="market.validation_universe.seeded", entity_type="market",
            entity_id="validation_universe", actor_type="human",
            actor_id="seed_validation_instruments",
            payload={"path": str(a.path), "inserted": list(res.inserted),
                     "already_present": list(res.already_present),
                     "is_active": False})
    print(f"validation universe: inserted={list(res.inserted)} "
          f"already_present={list(res.already_present)} (all is_active=FALSE)")
    print("these instruments are invisible to gates/scanner/desk by design; "
          "backfill them via: python -m atlas.dcp.market_data.backfill "
          "--symbols <SYM,...> --from <ISO> --end <ISO>")


if __name__ == "__main__":
    main()
