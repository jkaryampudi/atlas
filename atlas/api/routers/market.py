from __future__ import annotations

from fastapi import APIRouter

from sqlalchemy import text

from atlas.core.db import session_scope

router = APIRouter()


@router.get("/instruments")
def instruments(market: str | None = None) -> list[dict[str, object]]:
    q = "SELECT symbol, exchange, market, instrument_type, name, currency FROM market.instruments WHERE is_active"
    params: dict[str, str] = {}
    if market:
        q += " AND market = :m"
        params["m"] = market
    with session_scope() as s:
        return [dict(r) for r in s.execute(text(q + " ORDER BY symbol"), params).mappings()]


@router.get("/quality-gates")
def gates() -> list[dict[str, object]]:
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT DISTINCT ON (market) market, gate_date, status, reasons "
            "FROM market.data_quality_gates ORDER BY market, gate_date DESC")).mappings()
        return [{**dict(r), "gate_date": r["gate_date"].isoformat()} for r in rows]


@router.get("/freshness")
def freshness() -> list[dict[str, object]]:
    """Per-market data currency: latest real bar, counts, latest gate status."""
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT i.market, max(pb.bar_date) AS latest_bar, "
            "       count(*) AS bars, count(DISTINCT i.symbol) AS instruments "
            "FROM market.price_bars_daily pb "
            "JOIN market.instruments i ON i.id = pb.instrument_id "
            "WHERE pb.source = 'EodhdAdapter' "
            "GROUP BY i.market ORDER BY i.market")).mappings().all()
        latest_gates = {g["market"]: g for g in s.execute(text(
            "SELECT DISTINCT ON (market) market, gate_date, status "
            "FROM market.data_quality_gates ORDER BY market, gate_date DESC")).mappings()}
        return [{"market": r["market"], "latest_bar": r["latest_bar"].isoformat(),
                 "bars": r["bars"], "instruments": r["instruments"],
                 "latest_gate": latest_gates[r["market"]]["status"]
                 if r["market"] in latest_gates else None,
                 "gate_date": latest_gates[r["market"]]["gate_date"].isoformat()
                 if r["market"] in latest_gates else None} for r in rows]


@router.get("/bars/{symbol}")
def bars(symbol: str, days: int = 90) -> list[dict[str, object]]:
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT pb.bar_date, pb.open, pb.high, pb.low, pb.close, pb.volume "
            "FROM market.price_bars_daily pb "
            "JOIN market.instruments i ON i.id = pb.instrument_id "
            "WHERE i.symbol = :sym AND pb.source = 'EodhdAdapter' "
            "ORDER BY pb.bar_date DESC LIMIT :n"), {"sym": symbol, "n": days}).mappings()
        return [{"bar_date": r["bar_date"].isoformat(),
                 **{k: str(r[k]) for k in ("open", "high", "low", "close")},
                 "volume": r["volume"]} for r in reversed(list(rows))]


@router.get("/fx")
def fx(days: int = 30) -> list[dict[str, object]]:
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT base, quote, rate_date, rate FROM market.fx_rates_daily "
            "ORDER BY rate_date DESC LIMIT :n"), {"n": days}).mappings()
        return [{**dict(r), "rate_date": r["rate_date"].isoformat(),
                 "rate": str(r["rate"])} for r in reversed(list(rows))]
