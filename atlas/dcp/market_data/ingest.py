"""Daily ingestion: instruments seed, bars, quality gate — with audit events.

Expected days come from the exchange calendar (task 1a): the previous
`lookback_sessions` sessions plus the day itself when it is a session. A
non-trading day writes an explicit green gate instead of a false RED.
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.calendars import (is_trading_day, previous_trading_day,
                                              recent_sessions)
from atlas.dcp.market_data.models import Bar, Dividend, GateStatus, Split
from atlas.dcp.market_data.quality import GateResult, evaluate_gate, inception_map


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


def upsert_bar(session: Session, instrument_id: object, bar: Bar, source: str) -> None:
    session.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, high, low, close, volume, source, quality_flags) "
        "VALUES (:iid, :d, :o, :h, :l, :c, :v, :src, :qf) "
        "ON CONFLICT (instrument_id, bar_date) DO UPDATE SET "
        "  open=:o, high=:h, low=:l, close=:c, volume=:v, source=:src"),
        {"iid": instrument_id, "d": bar.bar_date, "o": bar.open, "h": bar.high,
         "l": bar.low, "c": bar.close, "v": bar.volume, "src": source,
         "qf": list(bar.quality_flags)})


def record_split(session: Session, instrument_id: object, split: Split, source: str) -> None:
    # Arbiter columns are required: a bare ON CONFLICT never matches the natural
    # key (migration 0005), so re-runs would duplicate splits and compound N× on
    # read-side adjustment (review finding).
    session.execute(text(
        "INSERT INTO market.corporate_actions "
        "(instrument_id, action_date, action_type, ratio, source) "
        "VALUES (:iid, :d, 'split', :r, :src) "
        "ON CONFLICT (instrument_id, action_date, action_type) DO NOTHING"),
        {"iid": instrument_id, "d": split.action_date, "r": split.ratio, "src": source})


def record_dividend(session: Session, instrument_id: object, div: Dividend,
                    source: str) -> None:
    """Cash dividend into market.corporate_actions: action_type='dividend' is
    already permitted by the 0001 CHECK constraint and the table carries
    dedicated `amount`/`currency` columns, so no schema change is needed —
    `ratio` stays NULL (that column is split semantics). Amount is the RAW
    declared cash per share (adjust on read, the bars convention). Same
    natural-key arbiter as record_split, so re-runs never duplicate."""
    session.execute(text(
        "INSERT INTO market.corporate_actions "
        "(instrument_id, action_date, action_type, amount, currency, source) "
        "VALUES (:iid, :d, 'dividend', :a, :cur, :src) "
        "ON CONFLICT (instrument_id, action_date, action_type) DO NOTHING"),
        {"iid": instrument_id, "d": div.ex_date, "a": div.amount,
         "cur": div.currency, "src": source})


def write_gate(session: Session, gate: GateResult) -> None:
    session.execute(text(
        "INSERT INTO market.data_quality_gates (market, gate_date, status, reasons) "
        "VALUES (:m, :d, :s, CAST(:r AS jsonb)) "
        "ON CONFLICT (market, gate_date) DO UPDATE SET status=:s, reasons=CAST(:r AS jsonb)"),
        {"m": gate.market, "d": gate.gate_date, "s": gate.status.value,
         "r": json.dumps(list(gate.reasons))})


def _non_trading_day_gate(session: Session, market: str, day: date) -> GateResult:
    """A non-trading day must never mask an unresolved problem: the latest-gate
    view (API/dashboard) reads only the newest row per market, so a weekend GREEN
    after a red Friday would silently unblock downstream work (review finding,
    critical). Carry the previous session's gate status forward instead."""
    prev = previous_trading_day(market, day)
    prev_status = session.execute(text(
        "SELECT status FROM market.data_quality_gates "
        "WHERE market = :m AND gate_date = :d"), {"m": market, "d": prev}).scalar()
    if prev_status is None:
        has_bars = session.execute(text(
            "SELECT 1 FROM market.price_bars_daily pb "
            "JOIN market.instruments i ON i.id = pb.instrument_id "
            "WHERE i.market = :m AND pb.bar_date = :d LIMIT 1"),
            {"m": market, "d": prev}).scalar()
        status = GateStatus.GREEN if has_bars else GateStatus.RED
        detail = (f"no gate for previous session {prev}; bars "
                  f"{'present' if has_bars else 'MISSING'}")
    else:
        status = GateStatus(prev_status)
        detail = f"carried forward from {prev}: {prev_status}"
    return GateResult(market=market, gate_date=day, status=status,
                      reasons=("non-trading day", detail))


def ingest_day(*, session: Session, adapter: MarketDataAdapter, audit: PostgresAuditLog,
               market: str, day: date, lookback_sessions: int = 1) -> GateStatus:
    if not is_trading_day(market, day):
        gate = _non_trading_day_gate(session, market, day)
        write_gate(session, gate)
        audit.append(event_type="market.bars.ingested", entity_type="market",
                     entity_id=market, actor_type="scheduler", actor_id="ingest_day",
                     payload={"market": market, "day": day.isoformat(), "instruments": 0,
                              "gate": gate.status.value, "reasons": list(gate.reasons)})
        return gate.status

    expected_days = recent_sessions(market, day, lookback=lookback_sessions)
    window_start = expected_days[0]
    instruments = session.execute(text(
        "SELECT id, symbol FROM market.instruments "
        "WHERE market = :m AND is_active"), {"m": market}).mappings().all()

    bars_by_day: dict[date, list[Bar]] = {}
    explained: set[str] = set()
    for inst in instruments:
        for sp in adapter.fetch_splits(inst["symbol"], window_start, day):
            explained.add(inst["symbol"])
            record_split(session, inst["id"], sp, type(adapter).__name__)
        for b in adapter.fetch_bars(inst["symbol"], window_start, day):
            bars_by_day.setdefault(b.bar_date, []).append(b)
            upsert_bar(session, inst["id"], b, type(adapter).__name__)

    # Rules v1.2: inception-filtered coverage, computed AFTER this day's upserts
    # so a brand-new symbol's first stored bar defines its inception. A symbol
    # with no stored bars at all stays fail-closed expected (honestly RED).
    gate = evaluate_gate(market=market, as_of=day, expected_days=expected_days,
                         bars_by_day=bars_by_day,
                         explained_symbols=frozenset(explained),
                         expected_symbols=frozenset(i["symbol"] for i in instruments),
                         inceptions=inception_map(session, market))
    write_gate(session, gate)

    audit.append(event_type="market.bars.ingested", entity_type="market", entity_id=market,
                 actor_type="scheduler", actor_id="ingest_day",
                 payload={"market": market, "day": day.isoformat(),
                          "instruments": len(instruments), "gate": gate.status.value,
                          "reasons": list(gate.reasons)})
    return gate.status
