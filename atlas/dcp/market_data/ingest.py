"""Daily ingestion: instruments seed, bars, FX, quality gate — with audit events."""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.models import Bar, GateStatus
from atlas.dcp.market_data.quality import evaluate_gate


def seed_instruments(session: Session, csv_path: Path) -> int:
    n = 0
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            session.execute(text(
                "INSERT INTO market.instruments "
                "(symbol, exchange, market, instrument_type, name, sector_gics, currency, "
                " economic_exposure) "
                "VALUES (:symbol, :exchange, :market, :instrument_type, :name, :sector_gics, "
                "        :currency, string_to_array(:economic_exposure, '|')) "
                "ON CONFLICT (symbol, exchange) DO NOTHING"), row)
            n += 1
    return n


def ingest_day(*, session: Session, adapter: MarketDataAdapter, audit: PostgresAuditLog,
               market: str, day: date, lookback_days: list[date]) -> GateStatus:
    instruments = session.execute(text(
        "SELECT id, symbol FROM market.instruments "
        "WHERE market = :m AND is_active"), {"m": market}).mappings().all()

    bars_by_day: dict[date, list[Bar]] = {}
    explained: set[str] = set()
    window_start = min(lookback_days + [day])
    for inst in instruments:
        for sp in adapter.fetch_splits(inst["symbol"], window_start, day):
            explained.add(inst["symbol"])
            session.execute(text(
                "INSERT INTO market.corporate_actions "
                "(instrument_id, action_date, action_type, ratio, source) "
                "VALUES (:iid, :d, 'split', :r, :src) ON CONFLICT DO NOTHING"),
                {"iid": inst["id"], "d": sp.action_date, "r": sp.ratio,
                 "src": type(adapter).__name__})
        bars = adapter.fetch_bars(inst["symbol"], window_start, day)
        for b in bars:
            bars_by_day.setdefault(b.bar_date, []).append(b)
            session.execute(text(
                "INSERT INTO market.price_bars_daily "
                "(instrument_id, bar_date, open, high, low, close, volume, source, "
                " quality_flags) "
                "VALUES (:iid, :d, :o, :h, :l, :c, :v, :src, :qf) "
                "ON CONFLICT (instrument_id, bar_date) DO UPDATE SET "
                "  open=:o, high=:h, low=:l, close=:c, volume=:v, source=:src"),
                {"iid": inst["id"], "d": b.bar_date, "o": b.open, "h": b.high, "l": b.low,
                 "c": b.close, "v": b.volume, "src": type(adapter).__name__,
                 "qf": list(b.quality_flags)})

    gate = evaluate_gate(market=market, as_of=day, expected_days=lookback_days + [day],
                         bars_by_day=bars_by_day,
                         explained_symbols=frozenset(explained))
    session.execute(text(
        "INSERT INTO market.data_quality_gates (market, gate_date, status, reasons) "
        "VALUES (:m, :d, :s, CAST(:r AS jsonb)) "
        "ON CONFLICT (market, gate_date) DO UPDATE SET status=:s, reasons=CAST(:r AS jsonb)"),
        {"m": market, "d": day, "s": gate.status.value,
         "r": json.dumps(list(gate.reasons))})

    audit.append(event_type="market.bars.ingested", entity_type="market", entity_id=market,
                 actor_type="scheduler", actor_id="ingest_day",
                 payload={"market": market, "day": day.isoformat(),
                          "instruments": len(instruments), "gate": gate.status.value,
                          "reasons": list(gate.reasons)})
    return gate.status
