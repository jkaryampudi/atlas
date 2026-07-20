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
from atlas.dcp.reporting.attribution import (
    cumulative_by_sleeve,
    satellite_sleeve_meta,
    scoped_performance,
)
from atlas.dcp.strategy_lifecycle import (
    ALL_SIMULATED,
    RESEARCH_SHADOW,
    RESEARCH_SHADOW_SCOPE,
    classify,
    is_authoritative,
    normalize_scope,
    validation_label,
)

router = APIRouter()

_SATELLITE = ("xsmom", "pead")
_ALL_SLEEVES = frozenset({"core", "xsmom", "pead", "cash", "total"})


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


def _visible_sleeves(scope: str, meta: dict[str, dict[str, str | None]]) -> frozenset[str]:
    """Which sleeve rows a scope shows (ADR-0018). authoritative excludes any
    PRESENT non-authoritative satellite (e.g. research_shadow); research_shadow
    shows ONLY the shadow satellites; all_simulated shows everything. Structural
    sleeves (core/cash/total) and an unallocated satellite (no strategy) stay in
    the authoritative view — they carry no shadow performance."""
    present_nonauth = frozenset(
        sl for sl in _SATELLITE
        if meta[sl]["state"] is not None and not is_authoritative(meta[sl]["state"]))
    shadow = frozenset(
        sl for sl in _SATELLITE if classify(meta[sl]["state"]) == RESEARCH_SHADOW)
    if scope == ALL_SIMULATED:
        return _ALL_SLEEVES
    if scope == RESEARCH_SHADOW_SCOPE:
        return shadow
    return _ALL_SLEEVES - present_nonauth


@router.get("/attribution/daily")
def attribution_daily(scope: str | None = None) -> object:
    """The ADR-0012 daily sleeve series + the SCOPED satellite composite
    (ADR-0018). Default performance_scope is authoritative_portfolio: the
    composite excludes research_shadow sleeves by construction, and shadow rows
    are omitted from the default view. `scope=research_shadow` returns the
    shadow-only view (labelled NOT VALIDATED); `scope=all_simulated` combines
    both (NON-AUTHORITATIVE, explicit request only). Read-only; empty series
    answer empty lists, never a 404."""
    try:
        scope = normalize_scope(scope)
    except ValueError as e:
        return _envelope(400, "INVALID_SCOPE", str(e))
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT session_date, sleeve, value_aud, ret_1d, benchmark_ret_1d "
            "FROM reporting.attribution_daily "
            "ORDER BY session_date, sleeve")).all()
        cumulative = cumulative_by_sleeve(s)
        meta = satellite_sleeve_meta(s)
        perf = scoped_performance(s, scope)

    visible = _visible_sleeves(scope, meta)

    def _label(sleeve: str) -> dict[str, object]:
        st = meta.get(sleeve, {}).get("state")
        return validation_label(st) if st is not None else {}

    return {
        # ADR-0018 scope envelope (the CALCULATION is separated, not just tagged)
        "performance_scope": perf["performance_scope"],
        "authoritative": perf["authoritative"],
        "validation_status": perf["validation_status"],
        "included_strategy_ids": perf["included_strategy_ids"],
        "excluded_strategy_ids": perf["excluded_strategy_ids"],
        "contains_shadow_results": perf["contains_shadow_results"],
        "artifact_digest": perf["artifact_digest"],
        "caveat": perf["caveat"],
        "satellite_alpha_pp": _dec(perf["satellite_alpha_pp"]),
        # backward-compatible composite flags (mirror the scope authoritativeness)
        "satellite_alpha_authoritative": perf["authoritative"],
        "satellite_alpha_validation_status": perf["validation_status"],
        "rows": [{"session_date": r.session_date.isoformat(),
                  "sleeve": r.sleeve,
                  "value_aud": _dec(r.value_aud),
                  "ret_1d": _dec(r.ret_1d),
                  "benchmark_ret_1d": _dec(r.benchmark_ret_1d),
                  **_label(r.sleeve)}
                 for r in rows if r.sleeve in visible],
        "cumulative": {c.sleeve: {"sessions": c.sessions,
                                  "ret_pct": _dec(c.ret_pct),
                                  "benchmark_pct": _dec(c.benchmark_pct),
                                  "excess_pp": _dec(c.excess_pp)}
                       for c in cumulative if c.sleeve in visible}}


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
