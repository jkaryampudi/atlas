"""Risk read surface (Doc 06: /risk/limit-sets/current, /risk/drawdown).

Read-only. The engine itself lives in atlas/dcp/risk; this router only reports
its governing configuration and state — honestly, including 'not effective yet'
and 'no NAV series yet' provenance.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from sqlalchemy import text

from atlas.core.db import session_scope

router = APIRouter()

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
    """Drawdown circuit-breaker ladder (Doc 04 §5). Level requires a NAV series
    and high-water mark, which arrive with Phase 5 paper trading — say so."""
    return {
        "current_level": "NONE",
        "provenance": "no NAV series yet — breaker state computes from Phase 5 "
                      "portfolio snapshots; DD2/DD3 latch until dual-confirmed "
                      "human clearance",
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
