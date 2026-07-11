"""Research surface (Doc 06): committee memos with their evidence trail, plus
the Principal's review write path — the ONE mutation this API performs, because
Doc 08 makes human memo review a phase gate and the sign-off must be
evidenceable. Memos themselves are written only by the agent runtime."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import SystemClock
from atlas.core.db import session_scope

router = APIRouter()

REVIEW_TARGET = 10  # Doc 08 Phase-2 gate: human reviews 10 memos


@router.get("/memos")
def memos(symbol: str | None = None, limit: int = 25) -> list[dict[str, object]]:
    q = ("SELECT m.id, m.memo_type, m.instrument_symbol, m.recommendation, "
         " m.conviction, m.thesis, m.kill_criteria, m.evidence_refs, m.dissent, "
         " m.debate_summary, m.created_at, r.model, r.status AS run_status, r.shadow, "
         " rev.verdict AS review_verdict, rev.notes AS review_notes, rev.reviewed_at "
         "FROM research.memos m "
         "LEFT JOIN research.agent_runs r ON r.id = m.agent_run_id "
         "LEFT JOIN research.memo_reviews rev ON rev.memo_id = m.id")
    params: dict[str, object] = {"n": limit}
    if symbol:
        q += " WHERE m.instrument_symbol = :sym"
        params["sym"] = symbol
    q += " ORDER BY m.created_at DESC LIMIT :n"
    with session_scope() as s:
        rows = s.execute(text(q), params).mappings()
        return [{**dict(r), "id": str(r["id"]),
                 "created_at": r["created_at"].isoformat(),
                 "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None}
                for r in rows]


class ReviewBody(BaseModel):
    verdict: str = Field(pattern="^(agree|disagree)$")
    notes: str = ""


@router.post("/memos/{memo_id}/review")
def review_memo(memo_id: str, body: ReviewBody) -> dict[str, object]:
    """Record the Principal's judgement on a memo (upsert; audited as a human
    action). This is deliberately the only write on the read surface."""
    with session_scope() as s:
        exists = s.execute(text("SELECT 1 FROM research.memos WHERE id = :i"),
                           {"i": memo_id}).scalar()
        if not exists:
            raise HTTPException(404, "memo not found")
        s.execute(text(
            "INSERT INTO research.memo_reviews (memo_id, verdict, notes) "
            "VALUES (:i, :v, :notes) "
            "ON CONFLICT (memo_id) DO UPDATE SET verdict=:v, notes=:notes, "
            " reviewed_at=now()"),
            {"i": memo_id, "v": body.verdict, "notes": body.notes})
        PostgresAuditLog(s, SystemClock()).append(
            event_type="memo.review.recorded", entity_type="memo",
            entity_id=memo_id, actor_type="human", actor_id="principal",
            payload={"verdict": body.verdict, "notes": body.notes[:500]})
        progress = s.execute(text(
            "SELECT count(*) FROM research.memo_reviews")).scalar()
    return {"ok": True, "reviewed": progress, "target": REVIEW_TARGET}


@router.get("/review-progress")
def review_progress() -> dict[str, object]:
    with session_scope() as s:
        n = s.execute(text("SELECT count(*) FROM research.memo_reviews")).scalar()
        agree = s.execute(text(
            "SELECT count(*) FROM research.memo_reviews WHERE verdict='agree'")).scalar()
    return {"reviewed": n, "agree": agree, "disagree": (n or 0) - (agree or 0),
            "target": REVIEW_TARGET}


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
