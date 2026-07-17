"""Portfolio surface (Doc 06 §2 resource map): latest snapshot, the monthly
attribution line (GET /portfolio/attribution/{period}, Doc 04 §14), and the
daily core/satellite sleeve series (GET /portfolio/attribution/daily,
ADR-0012 consequence 4 — read-only over reporting.attribution_daily).

House conventions: exact Decimal figures travel as STRINGS (no float drift),
and malformed input answers the Doc 06 §3.3 uniform error envelope
{error: {code, message, details}} exactly like the trading router.

ROUTE ORDER IS LOAD-BEARING: /attribution/daily is registered BEFORE
/attribution/{period} so the literal path wins the match — otherwise
'daily' would be swallowed as a malformed period.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text

from atlas.core.db import session_scope
from atlas.dcp.portfolio.attribution import compute_attribution
from atlas.dcp.reporting.attribution import cumulative_alpha_pp, cumulative_by_sleeve

router = APIRouter()


def _dec(v: object) -> str | None:
    """Exact Decimal as a plain string (never scientific notation — a stored
    0-scale-8 must read '0.00000000', not '0E-8')."""
    return None if v is None else format(Decimal(str(v)), "f")


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


def _envelope(status: int, code: str, message: str) -> JSONResponse:
    """Doc 06 §3.3 uniform error envelope (same shape as the trading router)."""
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message, "details": None}})


_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")


@router.get("/attribution/daily")
def attribution_daily() -> dict[str, object]:
    """The ADR-0012 consequence-4 daily sleeve series plus per-sleeve
    cumulative-vs-benchmark compounding and the satellite alpha number.
    Read-only; an empty series answers empty lists, never a 404 — the
    machinery exists before the history does."""
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT session_date, sleeve, value_aud, ret_1d, benchmark_ret_1d "
            "FROM reporting.attribution_daily "
            "ORDER BY session_date, sleeve")).all()
        cumulative = cumulative_by_sleeve(s)
        alpha = cumulative_alpha_pp(s)
    return {
        "rows": [{"session_date": r.session_date.isoformat(),
                  "sleeve": r.sleeve,
                  "value_aud": _dec(r.value_aud),
                  "ret_1d": _dec(r.ret_1d),
                  "benchmark_ret_1d": _dec(r.benchmark_ret_1d)}
                 for r in rows],
        "cumulative": {c.sleeve: {"sessions": c.sessions,
                                  "ret_pct": _dec(c.ret_pct),
                                  "benchmark_pct": _dec(c.benchmark_pct),
                                  "excess_pp": _dec(c.excess_pp)}
                       for c in cumulative},
        "satellite_alpha_pp": _dec(alpha)}


@router.get("/attribution/{period}")
def attribution(period: str) -> object:
    """Doc 04 §14 monthly attribution for period 'YYYY-MM'. Every figure is a
    quantized Decimal serialised as a string; absent measurements are null
    (see atlas.dcp.portfolio.attribution for the boundary conventions)."""
    m = _PERIOD_RE.fullmatch(period)
    if m is None:
        return _envelope(400, "INVALID_PERIOD",
                         f"period must be YYYY-MM, got {period!r}")
    try:
        with session_scope() as s:
            a = compute_attribution(s, year=int(m.group(1)), month=int(m.group(2)))
    except ValueError as e:  # month 00/13-99, year 0000 — datetime refuses
        return _envelope(400, "INVALID_PERIOD", f"{period!r}: {e}")
    body = asdict(a)
    for key in ("realised_pnl_aud", "nav_start_aud", "nav_end_aud",
                "unrealised_swing_aud", "llm_spend_usd"):
        body[key] = str(body[key]) if body[key] is not None else None
    for line in (body["entry_shortfall"], body["exit_shortfall"]):
        line["cost_aud"] = str(line["cost_aud"])
        line["avg_bps"] = str(line["avg_bps"]) if line["avg_bps"] is not None else None
    return body
