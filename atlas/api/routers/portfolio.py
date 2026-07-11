from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from atlas.core.db import session_scope

router = APIRouter()


@router.get("/snapshot")
def snapshot() -> dict[str, object]:
    with session_scope() as s:
        r = s.execute(text(
            "SELECT as_of, nav_aud, cash_aud, holdings, exposures, open_risk_pct "
            "FROM trading.portfolio_snapshots ORDER BY as_of DESC LIMIT 1")).mappings().first()
    if not r:
        raise HTTPException(404, "no snapshot yet")
    return {**dict(r), "as_of": r["as_of"].isoformat(),
            "nav_aud": str(r["nav_aud"]), "cash_aud": str(r["cash_aud"])}
