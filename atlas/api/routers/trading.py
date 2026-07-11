"""Trading surface (Doc 06 §2/§3): the approval desk.

GET list/detail render the full evidence bundle (§3.1) — proposal, memo
lineage, itemised risk-check results with value/limit, kill criteria. POST
approve runs the Doc 06 §3.2 server sequence in ONE transaction: verify not
expired -> RE-RUN the risk check on a fresh snapshot -> a now-FAIL commits the
void and answers 409 RISK_RECHECK_FAILED with the itemised results. The fresh
check is authoritative; the console can never rubber-stamp.

All state changes happen in atlas.dcp.trading.proposals (the compute plane)
and audit themselves; this layer only maps outcomes to the §3.3 uniform error
envelope {error: {code, message, details}}.

Paper mode v1: single principal on a local console — §3.2's step_up_token /
scope plumbing is deferred to the auth phase; acknowledged_risks IS enforced.
POST /settle is the ops trigger for the paper broker until the T0-T9 daily
pipeline ships (same precedent as POST /market/ingest/runs).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from atlas.core.clock import Clock, SystemClock
from atlas.core.db import session_scope
from atlas.dcp.trading import exits
from atlas.dcp.trading import proposals as lifecycle

router = APIRouter()


def _clock() -> Clock:
    """Seam for tests (CLAUDE.md invariant 6: injectable time). Production
    uses wall time; limit-set effective dates make that self-gating."""
    return SystemClock()


def _envelope(status: int, code: str, message: str,
              details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message, "details": details}})


def _value_error_response(e: ValueError) -> JSONResponse:
    msg = str(e)
    if "unknown" in msg:
        return _envelope(404, "NOT_FOUND", msg)
    if "acknowledged_risks" in msg:
        return _envelope(400, "RISKS_NOT_ACKNOWLEDGED", msg)
    return _envelope(409, "INVALID_STATE", msg)


_LIST_SQL = (
    "SELECT tp.id, tp.state, tp.action, tp.entry_price, tp.stop_loss, "
    " tp.target_price, tp.position_size, tp.position_value_aud, tp.expires_at, "
    " tp.created_at, tp.committee_memo_id, tp.risk_check_id, "
    " i.symbol, i.market, rc.verdict AS check_verdict, "
    " o.id AS order_id, o.state AS order_state "
    "FROM trading.trade_proposals tp "
    "LEFT JOIN market.instruments i ON i.id = tp.instrument_id "
    "LEFT JOIN risk.risk_checks rc ON rc.id = tp.risk_check_id "
    "LEFT JOIN trading.orders o ON o.proposal_id = tp.id ")


def _proposal_row(r: Any) -> dict[str, object]:
    return {**dict(r),
            "id": str(r["id"]),
            "committee_memo_id": str(r["committee_memo_id"]),
            "risk_check_id": str(r["risk_check_id"]) if r["risk_check_id"] else None,
            "order_id": str(r["order_id"]) if r["order_id"] else None,
            "expires_at": r["expires_at"].isoformat(),
            "created_at": r["created_at"].isoformat()}


@router.get("/proposals")
def proposals(state: str | None = None, limit: int = 50) -> list[dict[str, object]]:
    q, params = _LIST_SQL, {"n": limit}
    if state:
        q += "WHERE tp.state = :state "
        params["state"] = state
    q += "ORDER BY tp.created_at DESC LIMIT :n"
    with session_scope() as s:
        return [_proposal_row(r) for r in s.execute(text(q), params).mappings()]


@router.get("/proposals/{proposal_id}")
def proposal_detail(proposal_id: str) -> dict[str, object]:
    """The Doc 06 §3.1 evidence bundle: one screen, full lineage."""
    with session_scope() as s:
        row = s.execute(text(
            "SELECT tp.*, i.symbol, i.market AS instrument_market, "
            " m.thesis, m.kill_criteria AS memo_kill_criteria, m.recommendation, "
            " m.conviction, m.dissent, m.debate_summary "
            "FROM trading.trade_proposals tp "
            "LEFT JOIN market.instruments i ON i.id = tp.instrument_id "
            "LEFT JOIN research.memos m ON m.id = tp.committee_memo_id "
            "WHERE tp.id = :p"), {"p": proposal_id}).mappings().first()
        if row is None:
            raise HTTPException(404, "proposal not found")
        checks = s.execute(text(
            "SELECT id, verdict, check_kind, results, price_snapshot, "
            " limit_set_version, created_at "
            "FROM risk.risk_checks WHERE proposal_id = :p ORDER BY created_at"),
            {"p": proposal_id}).mappings().all()
        orders = s.execute(text(
            "SELECT id, state, side, qty, broker, created_at, submitted_at "
            "FROM trading.orders WHERE proposal_id = :p ORDER BY created_at"),
            {"p": proposal_id}).mappings().all()
        return {
            "id": str(row["id"]), "state": row["state"], "action": row["action"],
            "symbol": row["symbol"], "market": row["instrument_market"],
            "entry_price": row["entry_price"], "stop_loss": row["stop_loss"],
            "target_price": row["target_price"],
            "position_size": row["position_size"],
            "position_value_aud": row["position_value_aud"],
            "expires_at": row["expires_at"].isoformat(),
            "created_at": row["created_at"].isoformat(),
            "committee_memo_id": str(row["committee_memo_id"]),
            "signal_ids": [str(x) for x in (row["signal_ids"] or [])],
            "investment_thesis": row["thesis"],
            "recommendation": row["recommendation"],
            "confidence": row["conviction"],
            "dissent": row["dissent"], "debate_summary": row["debate_summary"],
            "kill_criteria": row["memo_kill_criteria"],
            "risk_checks": [{**dict(c), "id": str(c["id"]),
                             "created_at": c["created_at"].isoformat()}
                            for c in checks],
            "orders": [{**dict(o), "id": str(o["id"]),
                        "created_at": o["created_at"].isoformat(),
                        "submitted_at": o["submitted_at"].isoformat()
                        if o["submitted_at"] else None}
                       for o in orders],
        }


class ApproveBody(BaseModel):
    acknowledged_risks: bool = False


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str, body: ApproveBody) -> Any:
    """Doc 06 §3.2. The void on a now-FAIL COMMITS before the 409 goes out —
    the state change is the deliverable, the status code is the messenger."""
    try:
        with session_scope() as s:
            outcome = lifecycle.approve(
                s, _clock(), proposal_id=proposal_id,
                acknowledged_risks=body.acknowledged_risks)
    except ValueError as e:
        return _value_error_response(e)
    if outcome.status == "RISK_RECHECK_FAILED":
        return _envelope(409, "RISK_RECHECK_FAILED",
                         "fresh risk check FAILED — approval voided, terminal",
                         details={"failures": list(outcome.failures),
                                  "risk_check_id": outcome.risk_check_id})
    if outcome.status == "PROPOSAL_EXPIRED":
        return _envelope(409, "PROPOSAL_EXPIRED",
                         "proposal is past its 24h TTL — recorded as expired")
    return {"status": "approved", "proposal_id": outcome.proposal_id,
            "order_id": outcome.order_id, "risk_check_id": outcome.risk_check_id}


class RejectBody(BaseModel):
    reason: str


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: str, body: RejectBody) -> Any:
    try:
        with session_scope() as s:
            outcome = lifecycle.reject(s, _clock(),
                                       proposal_id=proposal_id, reason=body.reason)
    except ValueError as e:
        return _value_error_response(e)
    if outcome.status == "PROPOSAL_EXPIRED":
        return _envelope(409, "PROPOSAL_EXPIRED",
                         "proposal is past its 24h TTL — recorded as expired")
    return {"status": "rejected", "proposal_id": outcome.proposal_id}


class CancelBody(BaseModel):
    reason: str


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, body: CancelBody) -> Any:
    try:
        with session_scope() as s:
            lifecycle.cancel_order(s, _clock(), order_id=order_id,
                                   reason=body.reason)
    except ValueError as e:
        return _value_error_response(e)
    return {"status": "cancelled", "order_id": order_id}


@router.get("/orders")
def orders(state: str | None = None, limit: int = 50) -> list[dict[str, object]]:
    q = ("SELECT o.id, o.state, o.side, o.qty, o.broker, o.created_at, "
         " o.submitted_at, o.closed_at, tp.id AS proposal_id, i.symbol, "
         " e.fill_price, e.shortfall_bps, e.executed_at "
         "FROM trading.orders o "
         "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
         "LEFT JOIN market.instruments i ON i.id = tp.instrument_id "
         "LEFT JOIN trading.executions e ON e.order_id = o.id ")
    params: dict[str, object] = {"n": limit}
    if state:
        q += "WHERE o.state = :state "
        params["state"] = state
    q += "ORDER BY o.created_at DESC LIMIT :n"
    with session_scope() as s:
        return [{**dict(r), "id": str(r["id"]), "proposal_id": str(r["proposal_id"]),
                 "created_at": r["created_at"].isoformat(),
                 "submitted_at": r["submitted_at"].isoformat() if r["submitted_at"] else None,
                 "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
                 "executed_at": r["executed_at"].isoformat() if r["executed_at"] else None}
                for r in s.execute(text(q), params).mappings()]


class CloseBody(BaseModel):
    reason: str


@router.post("/positions/{position_id}/close")
def close_position(position_id: str, body: CloseBody) -> Any:
    """Discretionary exit ahead of the stop: creates an EXIT proposal that
    still needs the human seal on the approval desk — only stops are
    pre-authorized. Refuses while another exit is in flight (409)."""
    try:
        with session_scope() as s:
            res = exits.close_position(s, _clock(), position_id=position_id,
                                       reason=body.reason)
    except ValueError as e:
        return _value_error_response(e)
    return {"status": "pending_approval", "proposal_id": res.proposal_id,
            "qty": res.qty}


@router.get("/positions")
def positions(include_closed: bool = False, limit: int = 50) -> list[dict[str, object]]:
    """The book, marked at the latest ingested close (display only — risk
    marks fail closed in the engine; here a missing close shows as null)."""
    q = ("SELECT p.id, p.qty, p.avg_cost, p.currency, p.opened_at, p.closed_at, "
         " p.current_stop, p.thesis_memo_id, i.symbol, i.market, "
         " (SELECT close FROM market.price_bars_daily b "
         "  WHERE b.instrument_id = p.instrument_id AND b.source = 'EodhdAdapter' "
         "    AND b.close IS NOT NULL ORDER BY b.bar_date DESC LIMIT 1) AS last_close "
         "FROM trading.positions p "
         "LEFT JOIN market.instruments i ON i.id = p.instrument_id ")
    if not include_closed:
        q += "WHERE p.closed_at IS NULL "
    q += "ORDER BY p.opened_at DESC NULLS LAST LIMIT :n"
    with session_scope() as s:
        return [{**dict(r), "id": str(r["id"]),
                 "thesis_memo_id": str(r["thesis_memo_id"]) if r["thesis_memo_id"] else None,
                 "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
                 "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None}
                for r in s.execute(text(q), {"n": limit}).mappings()]


@router.post("/settle")
def settle() -> dict[str, object]:
    """Ops trigger (pipeline precursor): expire stale proposals, then fill
    every pending order whose session data has arrived. A lineage-verification
    RuntimeError propagates as 500 — that is an integrity incident, not a
    handleable outcome."""
    with session_scope() as s:
        clock = _clock()
        expired = lifecycle.expire_stale(s, clock)
        fills = lifecycle.settle_orders(s, clock)
    return {"expired": list(expired),
            "fills": [{"order_id": f.order_id, "execution_id": f.execution_id,
                       "fill_date": f.fill_date.isoformat(),
                       "fill_price": float(f.fill_price),
                       "shortfall_bps": float(f.shortfall_bps)} for f in fills]}
