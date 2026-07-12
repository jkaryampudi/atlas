from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from atlas.core.audit import ChainVerificationError
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import SystemClock
from atlas.core.db import session_scope

router = APIRouter()


@router.get("/events/verify")
def verify() -> dict[str, object]:
    """A broken chain is a STRUCTURED state, never a 500 — tampering must be
    distinguishable from an API outage (Doc 08 standing kill condition)."""
    with session_scope() as s:
        try:
            n = PostgresAuditLog(s, SystemClock()).verify()
            return {"chain": "ok", "events_verified": n,
                    "break_at_seq": None, "reason": None}
        except ChainVerificationError as e:
            return {"chain": "broken", "events_verified": None,
                    "break_at_seq": e.seq, "reason": e.reason}


@router.get("/events")
def events(limit: int = 50) -> list[dict[str, object]]:
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT seq, event_type, entity_type, entity_id, actor_type, created_at "
            "FROM audit.decision_events ORDER BY seq DESC LIMIT :n"), {"n": limit}).mappings()
        return [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]


def _not_found(proposal_id: str) -> JSONResponse:
    """Doc 06 §3.3 uniform envelope — same shape the trading router answers with."""
    return JSONResponse(status_code=404, content={"error": {
        "code": "NOT_FOUND", "message": f"unknown proposal {proposal_id}",
        "details": None}})


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


@router.get("/decisions/{proposal_id}/reconstruct")
def reconstruct(proposal_id: str) -> Any:
    """Doc 01 §8: 'Every decision, reconstructible forever' — the full lineage
    tree for one proposal (Doc 06 §2), top-down: proposal -> committee memo
    (with its agent run and the Principal's review) -> every risk check as
    itemised at the time -> approvals -> orders -> executions -> tax lots,
    closed by the audit chain's own account of the decision. Strictly a read:
    nothing here recomputes, everything is what was recorded."""
    try:
        pid = str(UUID(proposal_id))  # normalised: payload text comparison below
    except ValueError:
        return _not_found(proposal_id)
    with session_scope() as s:
        p = s.execute(text(
            "SELECT tp.*, i.symbol, i.market AS instrument_market "
            "FROM trading.trade_proposals tp "
            "LEFT JOIN market.instruments i ON i.id = tp.instrument_id "
            "WHERE tp.id = :p"), {"p": pid}).mappings().first()
        if p is None:
            return _not_found(proposal_id)
        proposal = {
            "id": str(p["id"]), "state": p["state"], "action": p["action"],
            "symbol": p["symbol"], "market": p["instrument_market"],
            "committee_memo_id": str(p["committee_memo_id"]),
            "signal_ids": [str(x) for x in (p["signal_ids"] or [])],
            "entry_price": p["entry_price"], "stop_loss": p["stop_loss"],
            "target_price": p["target_price"],
            "position_size": p["position_size"],
            "position_value_aud": p["position_value_aud"],
            "risk_check_id": str(p["risk_check_id"]) if p["risk_check_id"] else None,
            "confidence": p["confidence"], "quant_score": p["quant_score"],
            "risk_score": p["risk_score"], "risks": p["risks"],
            "thesis_summary": p["thesis_summary"],
            "expires_at": p["expires_at"].isoformat(),
            "created_at": p["created_at"].isoformat(),
        }

        m = s.execute(text(
            "SELECT m.id, m.memo_type, m.instrument_symbol, m.recommendation, "
            " m.conviction, m.thesis, m.kill_criteria, m.dissent, m.debate_summary, "
            " m.evidence_refs, m.agent_run_id, "
            " rev.verdict AS review_verdict, rev.notes AS review_notes, rev.reviewed_at, "
            " r.model, r.status AS run_status, r.tokens_in, r.tokens_out, r.cost_usd, "
            " left(r.prompt_template_hash, 10) AS template_hash_prefix, r.shadow "
            "FROM research.memos m "
            "LEFT JOIN research.memo_reviews rev ON rev.memo_id = m.id "
            "LEFT JOIN research.agent_runs r ON r.id = m.agent_run_id "
            "WHERE m.id = :m"), {"m": p["committee_memo_id"]}).mappings().first()
        memo = None if m is None else {
            "id": str(m["id"]), "type": m["memo_type"],
            "symbol": m["instrument_symbol"],
            "recommendation": m["recommendation"], "conviction": m["conviction"],
            "thesis": m["thesis"], "kill_criteria": m["kill_criteria"],
            "dissent": m["dissent"], "debate_summary": m["debate_summary"],
            "evidence_refs": m["evidence_refs"],
            "review": ({"verdict": m["review_verdict"], "notes": m["review_notes"],
                        "reviewed_at": _iso(m["reviewed_at"])}
                       if m["review_verdict"] is not None else None),
            "agent_run": ({"model": m["model"], "status": m["run_status"],
                           "tokens_in": m["tokens_in"], "tokens_out": m["tokens_out"],
                           "cost_usd": float(m["cost_usd"] or 0),
                           "template_hash_prefix": m["template_hash_prefix"],
                           "shadow": m["shadow"]}
                          if m["agent_run_id"] is not None else None),
        }

        risk_checks = [
            {"id": str(c["id"]), "kind": c["check_kind"], "verdict": c["verdict"],
             "limit_set_version": c["limit_set_version"],
             "results": c["results"],  # itemised jsonb, verbatim as stored
             "price_snapshot": c["price_snapshot"],
             "created_at": c["created_at"].isoformat()}
            for c in s.execute(text(
                "SELECT id, check_kind, verdict, limit_set_version, results, "
                " price_snapshot, created_at "
                "FROM risk.risk_checks WHERE proposal_id = :p ORDER BY created_at"),
                {"p": pid}).mappings()]

        approvals = [
            {"decision": a["decision"], "approver": a["approver"],
             "auth_method": a["auth_method"], "decided_at": _iso(a["decided_at"]),
             "approval_time_risk_check_id": str(a["approval_time_risk_check_id"])}
            for a in s.execute(text(
                "SELECT decision, approver, auth_method, decided_at, "
                " approval_time_risk_check_id "
                "FROM trading.approvals WHERE proposal_id = :p ORDER BY created_at"),
                {"p": pid}).mappings()]

        orders: list[dict[str, Any]] = []
        execution_ids: list[str] = []
        position_ids: list[str] = []
        for o in s.execute(text(
                "SELECT id, state, side, qty, order_type, broker, created_at, "
                " submitted_at, closed_at "
                "FROM trading.orders WHERE proposal_id = :p ORDER BY created_at"),
                {"p": pid}).mappings():
            executions: list[dict[str, Any]] = []
            for e in s.execute(text(
                    "SELECT id, fill_qty, fill_price, fees, fx_rate_used, "
                    " decision_price, shortfall_bps, executed_at "
                    "FROM trading.executions WHERE order_id = :o ORDER BY created_at"),
                    {"o": o["id"]}).mappings():
                execution_ids.append(str(e["id"]))
                lots = []
                for lot in s.execute(text(
                        "SELECT id, position_id, qty, cost_aud, acquired_at, "
                        " disposed_at, proceeds_aud "
                        "FROM trading.tax_lots WHERE execution_id = :e "
                        "ORDER BY created_at"), {"e": e["id"]}).mappings():
                    if lot["position_id"] is not None:
                        position_ids.append(str(lot["position_id"]))
                    lots.append({
                        "id": str(lot["id"]),
                        "position_id": str(lot["position_id"]) if lot["position_id"] else None,
                        "qty": lot["qty"], "cost_aud": lot["cost_aud"],
                        "acquired_at": _iso(lot["acquired_at"]),
                        "disposed_at": _iso(lot["disposed_at"]),
                        "proceeds_aud": lot["proceeds_aud"]})
                executions.append({
                    "id": str(e["id"]), "fill_qty": e["fill_qty"],
                    "fill_price": e["fill_price"], "fees": e["fees"],
                    "fx_rate_used": e["fx_rate_used"],
                    "decision_price": e["decision_price"],
                    "shortfall_bps": e["shortfall_bps"],
                    "executed_at": _iso(e["executed_at"]),
                    "tax_lots": lots})
            orders.append({
                "id": str(o["id"]), "state": o["state"], "side": o["side"],
                "qty": o["qty"], "order_type": o["order_type"],
                "broker": o["broker"],
                "created_at": o["created_at"].isoformat(),
                "submitted_at": _iso(o["submitted_at"]),
                "closed_at": _iso(o["closed_at"]),
                "executions": executions})

        # the chain's own account: every event that referenced the proposal in
        # its payload, plus events keyed to the proposal's own entities
        entity_ids = [pid, *(o["id"] for o in orders), *execution_ids, *position_ids]
        events = [
            {"seq": e["seq"], "event_type": e["event_type"],
             "actor_type": e["actor_type"], "actor_id": e["actor_id"],
             "created_at": e["created_at"].isoformat(),
             "payload": e["payload"]}  # jsonb verbatim — never re-shaped
            for e in s.execute(text(
                "SELECT seq, event_type, actor_type, actor_id, created_at, payload "
                "FROM audit.decision_events "
                "WHERE payload->>'proposal_id' = :p OR entity_id = ANY(:ids) "
                "ORDER BY seq"), {"p": pid, "ids": entity_ids}).mappings()]

        return {"proposal": proposal, "memo": memo, "risk_checks": risk_checks,
                "approvals": approvals, "orders": orders, "events": events}
