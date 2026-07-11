"""Research read surface (Doc 06): committee memos with their evidence trail.
Read-only — memos are written only by the agent runtime."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from atlas.core.db import session_scope

router = APIRouter()


@router.get("/memos")
def memos(symbol: str | None = None, limit: int = 25) -> list[dict[str, object]]:
    q = ("SELECT m.id, m.memo_type, m.instrument_symbol, m.recommendation, "
         " m.conviction, m.thesis, m.kill_criteria, m.evidence_refs, m.dissent, "
         " m.debate_summary, m.created_at, r.model, r.status AS run_status, r.shadow "
         "FROM research.memos m "
         "LEFT JOIN research.agent_runs r ON r.id = m.agent_run_id")
    params: dict[str, object] = {"n": limit}
    if symbol:
        q += " WHERE m.instrument_symbol = :sym"
        params["sym"] = symbol
    q += " ORDER BY m.created_at DESC LIMIT :n"
    with session_scope() as s:
        rows = s.execute(text(q), params).mappings()
        return [{**dict(r), "id": str(r["id"]),
                 "created_at": r["created_at"].isoformat()} for r in rows]


@router.get("/runs")
def runs(limit: int = 40) -> list[dict[str, object]]:
    """The flight recorder: every model call, pass or fail, with its cost."""
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT id, agent_role, status, model, tokens_in, tokens_out, cost_usd, "
            " shadow, left(prompt_template_hash, 10) AS template, created_at "
            "FROM research.agent_runs ORDER BY created_at DESC LIMIT :n"),
            {"n": limit}).mappings()
        return [{**dict(r), "id": str(r["id"]), "cost_usd": float(r["cost_usd"] or 0),
                 "created_at": r["created_at"].isoformat()} for r in rows]


@router.get("/cost")
def cost_today() -> dict[str, object]:
    from atlas.core.config import get_settings

    with session_scope() as s:
        spent = s.execute(text(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM research.agent_runs "
            "WHERE created_at::date = CURRENT_DATE")).scalar()
    cap = get_settings().daily_llm_budget_usd
    return {"spent_usd": float(spent or 0), "daily_cap_usd": cap,
            "remaining_usd": max(0.0, cap - float(spent or 0))}
