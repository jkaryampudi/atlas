"""Research surface (Doc 06): committee memos with their evidence trail, plus
the Principal's review write path — the ONE mutation this API performs, because
Doc 08 makes human memo review a phase gate and the sign-off must be
evidenceable. Memos themselves are written only by the agent runtime."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
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


def _memo_not_found(memo_id: str) -> JSONResponse:
    """Doc 06 §3.3 uniform envelope — same shape the audit/trading routers use."""
    return JSONResponse(status_code=404, content={"error": {
        "code": "NOT_FOUND", "message": f"unknown memo {memo_id}", "details": None}})


@router.get("/memos/{memo_id}/decision-flow")
def decision_flow(memo_id: str) -> Any:
    """One memo's journey through the funnel, stage by stage: SCANNER (why the
    desk looked) -> EVIDENCE (the exact text the agents read, migration 0013)
    -> DEBATE -> VERDICT -> BRIDGE -> SEAL. Strictly a read of what was
    recorded; a stage the record cannot support answers available=false with
    an honest note, never a reconstruction from today's data.

    Documented resolutions:
    - Scanner match uses created_at <= the memo's (nearest first): a same-cycle
      scan under a frozen clock can share the memo's timestamp; a strictly-
      earlier match would orphan the stage there. Ties break by seq DESC.
    - Structured bull/bear DebateCases are NOT persisted anywhere (run_agent
      stores only the output hash), so the debate stage carries the memo's own
      verbatim debate_summary + dissent — nothing more is invented.
    - A bridge SKIP renders as available=false with the recorded reason: the
      skip is an honest recorded outcome, and the flow shows it verbatim."""
    try:
        mid = str(UUID(memo_id))   # normalised for payload text comparison
    except ValueError:
        return _memo_not_found(memo_id)
    with session_scope() as s:
        m = s.execute(text(
            "SELECT m.id, m.memo_type, m.instrument_symbol, m.recommendation, "
            " m.conviction, m.thesis, m.kill_criteria, m.evidence_refs, m.dissent, "
            " m.debate_summary, m.created_at, m.agent_run_id, "
            " r.model, r.status AS run_status, r.cost_usd, r.shadow "
            "FROM research.memos m "
            "LEFT JOIN research.agent_runs r ON r.id = m.agent_run_id "
            "WHERE m.id = :m"), {"m": mid}).mappings().first()
        if m is None:
            return _memo_not_found(memo_id)
        symbol = m["instrument_symbol"]

        # --- SCANNER: the attention decision that routed the desk here
        scanner: dict[str, Any] = {
            "available": False, "note": "pre-scanner memo or not shortlisted"}
        if symbol:
            ev = s.execute(text(
                "SELECT payload, created_at FROM audit.decision_events "
                "WHERE event_type = 'scanner.completed' AND created_at <= :t "
                "  AND payload->'shortlist' @> CAST(:probe AS jsonb) "
                "ORDER BY created_at DESC, seq DESC LIMIT 1"),
                {"t": m["created_at"],
                 "probe": json.dumps([{"symbol": symbol}])}).mappings().first()
            if ev is not None:
                entry = next(e for e in ev["payload"]["shortlist"]
                             if e.get("symbol") == symbol)
                scanner = {"available": True,
                           "scanned_at": ev["created_at"].isoformat(),
                           "criteria_version": ev["payload"].get("criteria_version"),
                           "scanned": ev["payload"].get("scanned"),
                           "eligible": ev["payload"].get("eligible"),
                           "top_n": ev["payload"].get("top_n"),
                           "entry": entry}   # score + components, verbatim

        # --- EVIDENCE: the persisted bodies (memo_evidence, ordinal order)
        ev_rows = s.execute(text(
            "SELECT ordinal, ref, body FROM research.memo_evidence "
            "WHERE memo_id = :m ORDER BY ordinal"), {"m": mid}).mappings().all()
        evidence: dict[str, Any] = (
            {"available": True, "items": [dict(r) for r in ev_rows]} if ev_rows
            else {"available": False,
                  "note": "evidence bodies not recorded before this feature"})

        # --- DEBATE: verbatim memo fields (structured cases are not persisted)
        debate: dict[str, Any] = (
            {"available": True, "debate_summary": m["debate_summary"],
             "dissent": m["dissent"]}
            if (m["debate_summary"] or m["dissent"])
            else {"available": False,
                  "note": "no debate summary or dissent recorded on this memo"})

        # --- VERDICT: the committee's answer, with its flight-recorder row
        verdict: dict[str, Any] = {
            "available": True, "recommendation": m["recommendation"],
            "conviction": m["conviction"], "thesis": m["thesis"],
            "kill_criteria": m["kill_criteria"],
            "evidence_refs": m["evidence_refs"],
            "agent_run": ({"model": m["model"], "status": m["run_status"],
                           "cost_usd": float(m["cost_usd"] or 0),
                           "shadow": m["shadow"]}
                          if m["agent_run_id"] is not None else None)}

        # --- BRIDGE: the deterministic memo->proposal outcome
        p = s.execute(text(
            "SELECT id, state, action, position_size, position_value_aud, "
            " entry_price, stop_loss, target_price, created_at "
            "FROM trading.trade_proposals WHERE committee_memo_id = :m "
            "ORDER BY created_at DESC LIMIT 1"), {"m": mid}).mappings().first()
        if p is not None:
            bridge: dict[str, Any] = {"available": True, "proposal": {
                "id": str(p["id"]), "state": p["state"], "action": p["action"],
                "qty": p["position_size"],
                "position_value_aud": p["position_value_aud"],
                "entry_price": p["entry_price"], "stop_loss": p["stop_loss"],
                "target_price": p["target_price"],
                "created_at": p["created_at"].isoformat()}}
        else:
            skip = s.execute(text(
                "SELECT payload FROM audit.decision_events "
                "WHERE event_type = 'trading.bridge.completed' "
                "  AND payload->'skipped' @> CAST(:probe AS jsonb) "
                "ORDER BY seq DESC LIMIT 1"),
                {"probe": json.dumps([{"memo_id": mid}])}).mappings().first()
            if skip is not None:
                reason = next(k.get("reason") for k in skip["payload"]["skipped"]
                              if k.get("memo_id") == mid)
                bridge = {"available": False, "note": f"bridge skipped: {reason}"}
            else:
                bridge = {"available": False, "note": "not bridged"}

        # --- SEAL: the human decision on the bridged proposal, if one exists
        if p is not None:
            approvals = [
                {"decision": a["decision"], "approver": a["approver"],
                 "auth_method": a["auth_method"],
                 "decided_at": a["decided_at"].isoformat() if a["decided_at"] else None}
                for a in s.execute(text(
                    "SELECT decision, approver, auth_method, decided_at "
                    "FROM trading.approvals WHERE proposal_id = :p "
                    "ORDER BY created_at"), {"p": p["id"]}).mappings()]
            status = {"draft": "awaiting", "risk_review": "awaiting",
                      "pending_approval": "awaiting", "approved": "approved",
                      "executed": "approved"}.get(p["state"], p["state"])
            seal: dict[str, Any] = {"available": True, "state": p["state"],
                                    "status": status, "approvals": approvals}
        else:
            seal = {"available": False,
                    "note": "no proposal — nothing reached the seal"}

        return {"memo_id": mid, "symbol": symbol,
                "memo_type": m["memo_type"],
                "created_at": m["created_at"].isoformat(),
                "stages": {"scanner": scanner, "evidence": evidence,
                           "debate": debate, "verdict": verdict,
                           "bridge": bridge, "seal": seal}}


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
