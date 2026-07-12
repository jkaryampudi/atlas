"""Risk surface (Doc 06: /risk/limit-sets/current, /risk/drawdown, and the
dual-confirmation breaker-clearance flow).

Reads report the engine's governing configuration and state — honestly,
including 'not effective yet' and 'no NAV series yet' provenance. The ONE
write path is the Doc 04 §5 resumption action: POST /breaker-clearances
(confirmation A) and POST /breaker-clearances/{id}/confirm (confirmation B,
≥1h later — DUAL_CONFIRM_TOO_SOON otherwise, Doc 06 §3.3). State changes
happen in atlas.dcp.risk.clearance and audit themselves; this layer only
maps outcomes to the §3.3 uniform error envelope, exactly like the trading
router.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from atlas.core.clock import Clock, SystemClock
from atlas.core.db import session_scope
from atlas.dcp.risk import clearance

router = APIRouter()


def _clock() -> Clock:
    """Seam for tests (CLAUDE.md invariant 6: injectable time). Production
    uses wall time; the ≥1h dual-confirmation gap makes that self-gating."""
    return SystemClock()


def _envelope(status: int, code: str, message: str,
              details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message, "details": details}})


def _value_error_response(e: ValueError) -> JSONResponse:
    msg = str(e)
    if "unknown" in msg:
        return _envelope(404, "NOT_FOUND", msg)
    if "DUAL_CONFIRM_TOO_SOON" in msg:
        return _envelope(409, "DUAL_CONFIRM_TOO_SOON", msg)
    return _envelope(409, "INVALID_STATE", msg)

# Doc 04 §3 — rule descriptions for the register (values come from the DB)
RULES: list[tuple[str, str, str]] = [
    ("L1", "L1_max_stock_weight", "Max single-stock weight at cost"),
    ("L2", "L2_max_etf_weight", "Max single-ETF weight"),
    ("L3", "L3_max_sector_exposure", "Max GICS sector exposure (pro-forma)"),
    ("L4", "L4_max_india_sleeve", "Max India sleeve incl. ADR/ETF look-through"),
    ("L5", "L5_min_cash_reserve", "Min cash reserve"),
    ("L6", "L6_max_risk_per_trade", "Max portfolio risk per trade (entry−stop × size)"),
    ("L7", "L7_max_aggregate_open_risk", "Max aggregate open risk to stops"),
    ("L8", "L8_corr_threshold", "Pairwise correlation threshold (with combined-weight cap)"),
    ("L9", "L9_max_new_positions_per_day", "Max new positions per day"),
    ("L10", "L10_max_pct_adv", "Max position vs 20-day ADV"),
    ("L11", "L11_max_non_aud_exposure", "Max unhedged non-AUD exposure"),
]


@router.get("/limit-set/current")
def limit_set_current() -> dict[str, object]:
    with session_scope() as s:
        row = s.execute(text(
            "SELECT version, mode, limits, effective_from, created_by "
            "FROM risk.limit_sets ORDER BY version DESC LIMIT 1")).mappings().first()
    if row is None:
        return {"seeded": False, "active": False, "register": []}
    limits = dict(row["limits"])
    eff: date = row["effective_from"]
    register = [{"rule": r, "description": d, "value": limits.get(k)}
                for r, k, d in RULES]
    register.append({"rule": "L8b", "description": "L8 combined-weight cap",
                     "value": limits.get("L8_corr_combined_weight")})
    return {"seeded": True,
            "version": row["version"], "mode": row["mode"],
            "effective_from": eff.isoformat(),
            "active": eff <= date.today(),
            "created_by": row["created_by"],
            "register": register}


@router.get("/breakers")
def breakers() -> dict[str, object]:
    """Drawdown circuit-breaker ladder (Doc 04 §5) plus the CURRENT latched
    level: the fold over persisted NAV snapshots + confirmed clearances (the
    book-independent view — no live mark, so this read can never fail on a
    stale close)."""
    with session_scope() as s:
        n = s.execute(text(
            "SELECT count(*) FROM trading.portfolio_snapshots")).scalar_one()
        level = clearance.latched_breaker_level(s)
    provenance = (
        "no NAV series yet — breaker state computes from portfolio snapshots; "
        "DD2/DD3 latch until dual-confirmed human clearance" if n == 0 else
        f"latched fold over {n} NAV snapshots; DD2/DD3 latch until "
        "dual-confirmed human clearance (Doc 04 §5)")
    return {
        "current_level": level.value.upper(),
        "provenance": provenance,
        "ladder": [
            {"level": "DD1", "trigger_pct": -5,
             "action": "New-position risk halved (L6 → 0.5%); CIO review memo required"},
            {"level": "DD2", "trigger_pct": -10,
             "action": "No new positions; full-book re-underwrite; human review to resume"},
            {"level": "DD3", "trigger_pct": -15,
             "action": "FULL HALT — exit-only; per-holding human keep/exit decision; "
                       "post-mortem before re-arming"},
        ],
    }


class ClearanceRequestBody(BaseModel):
    reason: str


@router.post("/breaker-clearances")
def request_breaker_clearance(body: ClearanceRequestBody) -> Any:
    """Confirmation A of the Doc 04 §5 resumption action. 409 INVALID_STATE
    when there is nothing to clear (breaker NONE/DD1) or a request is already
    pending."""
    try:
        with session_scope() as s:
            cid = clearance.request_clearance(s, _clock(), reason=body.reason)
    except ValueError as e:
        return _value_error_response(e)
    return {"status": "pending_confirmation", "clearance_id": cid}


@router.post("/breaker-clearances/{clearance_id}/confirm")
def confirm_breaker_clearance(clearance_id: str) -> Any:
    """Confirmation B. 409 DUAL_CONFIRM_TOO_SOON before requested_at + 1h
    (Doc 06 §3.3); 404 for an unknown id; 409 INVALID_STATE if already
    confirmed. Returns the recomputed latched level."""
    try:
        with session_scope() as s:
            level = clearance.confirm_clearance(s, _clock(),
                                                clearance_id=clearance_id)
    except ValueError as e:
        return _value_error_response(e)
    return {"status": "cleared", "clearance_id": clearance_id,
            "latched_level": level.value}


@router.get("/breaker-clearances")
def breaker_clearances(limit: int = 20) -> list[dict[str, object]]:
    """Recent clearance requests, pending first — the console renders the
    pending one with its not-before instant."""
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT id, from_level, reason, requested_by, requested_at, "
            "       confirmed_at "
            "FROM risk.breaker_clearances "
            "ORDER BY (confirmed_at IS NULL) DESC, requested_at DESC "
            "LIMIT :n"), {"n": limit}).mappings().all()
    return [{"id": str(r["id"]), "from_level": r["from_level"],
             "reason": r["reason"], "requested_by": r["requested_by"],
             "requested_at": r["requested_at"].isoformat(),
             "confirmable_after": (r["requested_at"]
                                   + clearance.DUAL_CONFIRM_GAP).isoformat(),
             "confirmed_at": (r["confirmed_at"].isoformat()
                              if r["confirmed_at"] else None),
             "pending": r["confirmed_at"] is None}
            for r in rows]
