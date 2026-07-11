"""Tradable-universe manifest sync (seeds/universe.json).

"What do we trade" is a reviewed file in git, not hand-run SQL: sync_universe
upserts instruments from the manifest and NEVER deletes or deactivates —
removing an instrument from trading is a human decision executed deliberately
(is_active is untouched, so a deactivated instrument stays deactivated even if
it remains in the manifest). Only descriptive fields (name, sector_gics,
economic_exposure) are updated on conflict; identity fields (market, type,
currency) changing would be a different instrument, not an edit.

Usage: python -m atlas.dcp.market_data.universe [--path seeds/universe.json]
A newly inserted instrument has no bars: the nightly ingest reports it as
needs_backfill and gates go RED until the deliberate backfill is run.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[3]

REQUIRED_FIELDS = ("symbol", "exchange", "market", "instrument_type", "name",
                   "sector_gics", "currency", "economic_exposure")


@dataclass(frozen=True)
class UniverseSyncResult:
    inserted: tuple[str, ...]  # "SYMBOL@EXCHANGE"
    updated: tuple[str, ...]
    unchanged: int


def sync_universe(session: Session, path: Path) -> UniverseSyncResult:
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError(f"universe manifest {path} must be a JSON array")

    inserted, updated, unchanged = [], [], 0
    for e in entries:
        missing = [k for k in REQUIRED_FIELDS if k not in e]
        if missing:
            raise ValueError(f"universe entry {e.get('symbol', '?')!r} "
                             f"missing fields: {missing}")
        key = f"{e['symbol']}@{e['exchange']}"
        row = session.execute(text(
            "SELECT name, sector_gics, economic_exposure FROM market.instruments "
            "WHERE symbol = :symbol AND exchange = :exchange"),
            {"symbol": e["symbol"], "exchange": e["exchange"]}).mappings().first()
        if row is None:
            session.execute(text(
                "INSERT INTO market.instruments "
                "(symbol, exchange, market, instrument_type, name, sector_gics, "
                " currency, economic_exposure) "
                "VALUES (:symbol, :exchange, :market, :instrument_type, :name, "
                "        :sector_gics, :currency, :economic_exposure) "
                "ON CONFLICT (symbol, exchange) DO NOTHING"),
                {k: e[k] for k in REQUIRED_FIELDS})
            inserted.append(key)
        elif ((row["name"], row["sector_gics"], list(row["economic_exposure"]))
              != (e["name"], e["sector_gics"], list(e["economic_exposure"]))):
            session.execute(text(
                "UPDATE market.instruments SET name = :name, sector_gics = :sector_gics, "
                "economic_exposure = :economic_exposure "
                "WHERE symbol = :symbol AND exchange = :exchange"),
                {"symbol": e["symbol"], "exchange": e["exchange"], "name": e["name"],
                 "sector_gics": e["sector_gics"],
                 "economic_exposure": e["economic_exposure"]})
            updated.append(key)
        else:
            unchanged += 1
    return UniverseSyncResult(inserted=tuple(inserted), updated=tuple(updated),
                              unchanged=unchanged)


def main() -> None:
    from atlas.core.audit_repo import PostgresAuditLog
    from atlas.core.clock import SystemClock
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(description="Sync the tradable-universe manifest "
                                            "into market.instruments (upsert, never delete)")
    p.add_argument("--path", type=Path, default=ROOT / "seeds" / "universe.json")
    a = p.parse_args()

    with session_scope() as s:
        res = sync_universe(s, a.path)
        PostgresAuditLog(s, SystemClock()).append(
            event_type="market.universe.synced", entity_type="market",
            entity_id="universe", actor_type="human", actor_id="sync_universe",
            payload={"path": str(a.path), "inserted": list(res.inserted),
                     "updated": list(res.updated), "unchanged": res.unchanged})
    print(f"universe sync: inserted={list(res.inserted)} updated={list(res.updated)} "
          f"unchanged={res.unchanged}")
    if res.inserted:
        print("newly inserted instruments have no bars — run the deliberate backfill "
              "(python -m atlas.dcp.market_data.backfill) or nightly gates go RED")


if __name__ == "__main__":
    main()
