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
