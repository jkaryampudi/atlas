"""Research surface (Doc 06): committee memos with their evidence trail, plus
the Principal's review write path — the ONE mutation this API performs, because
Doc 08 makes human memo review a phase gate and the sign-off must be
evidenceable. Memos themselves are written only by the agent runtime.

POST /analyze is a trigger, not a mutation: it hands a ticker to the ops
layer (atlas/ops/analyze.py, mirroring /v1/system/run-daily -> scheduler);
every resulting write goes through the full agent cage."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import SystemClock
from atlas.core.db import session_scope
from atlas.dcp.research.autopsy import compute_autopsy
from atlas.dcp.research.financials_panel import compute_financials
from atlas.dcp.research.health_score import compute_health_score
from atlas.dcp.research.stock_models import compute_models
from atlas.dcp.research.valuation_models import compute_valuation
from atlas.dcp.scorecard import dartboard_baseline, dissent_right, vindicated

router = APIRouter()

REVIEW_TARGET = 10  # Doc 08 Phase-2 gate: human reviews 10 memos
SCORECARD_RECENT = 20  # last N matured outcome rows on the scorecard


@router.get("/memos")
def memos(symbol: str | None = None, limit: int = 25) -> list[dict[str, object]]:
    # outcome_20: the memo's 20-session scorecard row (migration 0016) when it
    # has matured — the console's badge. At most one row per memo (UNIQUE on
    # memo_id, horizon_sessions), so the join never fans out. Floats are fine:
    # display analytics, not ledger money.
    q = ("SELECT m.id, m.memo_type, m.instrument_symbol, m.recommendation, "
         " m.conviction, m.thesis, m.kill_criteria, m.evidence_refs, m.dissent, "
         " m.debate_summary, m.source, "
         " m.created_at, r.model, r.status AS run_status, r.shadow, "
         " rev.verdict AS review_verdict, rev.notes AS review_notes, rev.reviewed_at, "
         " o20.excess AS o20_excess, o20.fwd_return AS o20_fwd_return, "
         " o20.spy_return AS o20_spy_return "
         "FROM research.memos m "
         "LEFT JOIN research.agent_runs r ON r.id = m.agent_run_id "
         "LEFT JOIN research.memo_reviews rev ON rev.memo_id = m.id "
         "LEFT JOIN research.memo_outcomes o20 "
         "  ON o20.memo_id = m.id AND o20.horizon_sessions = 20")
    params: dict[str, object] = {"n": limit}
    if symbol:
        q += " WHERE m.instrument_symbol = :sym"
        params["sym"] = symbol
    q += " ORDER BY m.created_at DESC LIMIT :n"
    with session_scope() as s:
        out: list[dict[str, object]] = []
        for r in s.execute(text(q), params).mappings():
            d = dict(r)
            excess = d.pop("o20_excess")
            fwd, spy = d.pop("o20_fwd_return"), d.pop("o20_spy_return")
            d["outcome_20"] = None if excess is None else {
                "excess": float(excess), "fwd_return": float(fwd),
                "spy_return": float(spy),
                "vindicated": vindicated(r["recommendation"], excess,
                                         shadow=bool(r["shadow"]))}
            out.append({**d, "id": str(r["id"]),
                        "created_at": r["created_at"].isoformat(),
                        "reviewed_at": (r["reviewed_at"].isoformat()
                                        if r["reviewed_at"] else None)})
        return out


def _memo_not_found(memo_id: str) -> JSONResponse:
    """Doc 06 §3.3 uniform envelope — same shape the audit/trading routers use."""
    return JSONResponse(status_code=404, content={"error": {
        "code": "NOT_FOUND", "message": f"unknown memo {memo_id}", "details": None}})


# ---- ANALYZE-ANY-TICKER: on-demand desk analysis (ops layer does the work) --

_ANALYZE_SYMBOL = re.compile(r"^[A-Z0-9.\-]{1,10}$")
ANALYZE_SOURCE_MAX = 40


class AnalyzeBody(BaseModel):
    symbol: str
    source: str | None = None


def _bad_request(code: str, message: str) -> JSONResponse:
    """Doc 06 §3.3 uniform envelope, 400 flavour."""
    return JSONResponse(status_code=400, content={"error": {
        "code": code, "message": message, "details": None}})


@router.post("/analyze")
def analyze(body: AnalyzeBody) -> Any:
    """Queue one on-demand desk analysis. `symbol` is upcased then validated;
    `source` is the optional external-origin tag (e.g. 'investing.com'),
    stored VERBATIM up to ANALYZE_SOURCE_MAX chars — it never enters a prompt
    (see cio.py), so no sanitisation beyond the length cap is needed. A busy
    desk answers {started: false} honestly — never an error, nothing runs
    twice (same contract as /v1/system/run-daily)."""
    from atlas.ops.analyze import start_analysis

    symbol = body.symbol.strip().upper()
    if not _ANALYZE_SYMBOL.fullmatch(symbol):
        return _bad_request("INVALID_SYMBOL",
                            "symbol must match ^[A-Z0-9.\\-]{1,10}$ after upcasing")
    source = body.source or None  # blank tag -> NULL (the desk's own work)
    if source is not None and len(source) > ANALYZE_SOURCE_MAX:
        return _bad_request("INVALID_SOURCE",
                            f"source tag exceeds {ANALYZE_SOURCE_MAX} characters")
    started = start_analysis(symbol, source)
    return {"started": started,
            "note": (f"analysis running for {symbol} — poll "
                     "/v1/research/analyze/status" if started
                     else "an analysis is already running — one at a time; "
                          "nothing started twice")}


@router.get("/analyze/status")
def analyze_status() -> dict[str, object]:
    """The current/last analysis: phase fetching -> analyzing -> done|failed,
    with the memo outcome or the honest cage-hold/skip detail."""
    from atlas.ops.analyze import analysis_status

    return analysis_status()


# ---- SOURCE PICKS: monthly external-list ingest + edge (measurement only) ---

PICK_MAX_TICKERS = 100


class PickIngestBody(BaseModel):
    source: str
    date: str | None = None                       # YYYY-MM-DD; default today UTC
    tickers: list[str] = Field(default_factory=list)
    run_desk: bool = False


@router.post("/source-picks/ingest")
def source_picks_ingest(body: PickIngestBody) -> Any:
    """Queue a monthly source-pick list (no command line). Each ticker gets a
    point-in-time feature snapshot; nothing becomes a trade (invariant 2 —
    picks are measured, never bridged). `run_desk` optionally adds a real
    committee memo per pick under the analyze budget. Busy answers
    {started:false} honestly — one ingest at a time."""
    from atlas.ops.ingest_picks import start_ingest_job

    source = body.source.strip()
    if not source or len(source) > ANALYZE_SOURCE_MAX:
        return _bad_request("INVALID_SOURCE",
                            f"source is required, max {ANALYZE_SOURCE_MAX} chars")
    try:
        rec_date = (datetime.strptime(body.date, "%Y-%m-%d").date() if body.date
                    else datetime.now(UTC).date())
    except ValueError:
        return _bad_request("INVALID_DATE", "date must be YYYY-MM-DD")
    seen: set[str] = set()
    tickers: list[str] = []
    for raw in body.tickers:
        t = raw.strip().upper()
        if not t or t in seen:
            continue
        if not _ANALYZE_SYMBOL.fullmatch(t):
            return _bad_request("INVALID_SYMBOL",
                                f"'{t}' must match ^[A-Z0-9.\\-]{{1,10}}$")
        seen.add(t)
        tickers.append(t)
    if not tickers:
        return _bad_request("NO_TICKERS", "provide at least one ticker")
    if len(tickers) > PICK_MAX_TICKERS:
        return _bad_request("TOO_MANY", f"max {PICK_MAX_TICKERS} tickers per list")
    started = start_ingest_job(source, rec_date, tickers, body.run_desk)
    return {"started": started, "n_tickers": len(tickers),
            "note": (f"ingesting {len(tickers)} pick(s) for {source} {rec_date} — "
                     "poll /v1/research/source-picks/ingest/status" if started
                     else "an ingest is already running — one at a time")}


@router.get("/source-picks/ingest/status")
def source_picks_ingest_status() -> dict[str, object]:
    from atlas.ops.ingest_picks import ingest_status

    return ingest_status()


@router.post("/source-picks/grade")
def source_picks_grade() -> dict[str, object]:
    """Grade every matured pick (excess vs SPY at 20/60 sessions, write-once)
    and return the per-source edge — outperform-rate against the dartboard.
    Near-zero edge is the honest verdict that the source has no skill."""
    from atlas.dcp.research.source_picks import grade_picks, source_edge_report

    with session_scope() as s:
        g = grade_picks(s, SystemClock())
        edge = [{"source": e.source, "horizon": e.horizon,
                 "n_matured": e.n_matured, "outperform_rate": e.outperform_rate,
                 "dartboard": e.dartboard, "edge": e.edge}
                for e in source_edge_report(s)]
    return {"graded": g.graded, "still_immature": g.still_immature, "edge": edge}


@router.get("/source-picks/edge")
def source_picks_edge() -> list[dict[str, object]]:
    """Read-only per-source edge (no grading side-effect) — for passive console
    display. POST /source-picks/grade is what matures new outcomes first."""
    from atlas.dcp.research.source_picks import source_edge_report

    with session_scope() as s:
        return [{"source": e.source, "horizon": e.horizon, "n_matured": e.n_matured,
                 "outperform_rate": e.outperform_rate, "dartboard": e.dartboard,
                 "edge": e.edge} for e in source_edge_report(s)]


@router.get("/source-picks")
def source_picks_list(source: str | None = None, limit: int = 200) -> list[dict[str, object]]:
    """Recorded picks, newest first, with their forward-return outcome and a
    few headline features for the console table. Read-only."""
    q = ("SELECT id, source, ticker, recommendation_date, as_of_session, "
         " source_recommendation, excess_5, excess_10, excess_20, excess_60, "
         " features, created_at "
         "FROM research.source_picks")
    params: dict[str, object] = {"n": max(1, min(limit, 1000))}
    if source:
        q += " WHERE source = :src"
        params["src"] = source
    q += " ORDER BY recommendation_date DESC, ticker LIMIT :n"
    out: list[dict[str, object]] = []
    with session_scope() as s:
        for r in s.execute(text(q), params).mappings():
            f = r["features"] or {}
            out.append({
                "id": str(r["id"]), "source": r["source"], "ticker": r["ticker"],
                "recommendation_date": r["recommendation_date"].isoformat(),
                "source_recommendation": r["source_recommendation"],
                "excess_5": (float(r["excess_5"]) if r["excess_5"] is not None else None),
                "excess_10": (float(r["excess_10"]) if r["excess_10"] is not None else None),
                "excess_20": (float(r["excess_20"]) if r["excess_20"] is not None else None),
                "excess_60": (float(r["excess_60"]) if r["excess_60"] is not None else None),
                "sector": f.get("sector_gics"), "mom_12_1": f.get("mom_12_1"),
                "ret_20d": f.get("ret_20d"), "trailing_pe": f.get("trailing_pe"),
                "spy_regime": f.get("spy_regime")})
    return out


def _compose_dossier(s: Any, instrument_id: Any, ticker: str, *,
                     pick: Any, models_as_of: Any) -> dict[str, Any]:
    """Assemble the full dossier for a name — the four Atlas panels (models,
    financials, valuation, health), the latest committee memo, the live signal
    flags, and a cross-check. Works WITH a source pick (external suggestion: adds
    its PIT features + SPY-relative outcome) or WITHOUT one (an Atlas suggestion —
    a memo/signal/proposal name — viewed present-tense)."""
    today = SystemClock().now().date()
    has_iid = instrument_id is not None
    models = compute_models(s, instrument_id, ticker, models_as_of) if has_iid else None
    financials = compute_financials(s, instrument_id, ticker, today) if has_iid else None
    valuation = compute_valuation(s, instrument_id, ticker, today) if has_iid else None
    health = compute_health_score(s, instrument_id, ticker, today) if has_iid else None
    # fragility markers ("pick autopsy"): a pure derivation of WHY this name looks
    # fragile, from the panels just computed — descriptive, never a filter.
    autopsy = compute_autopsy(models, valuation)

    memo = s.execute(text(
        "SELECT recommendation, conviction, thesis, dissent, kill_criteria, "
        " source, created_at FROM research.memos "
        "WHERE instrument_symbol = :t ORDER BY created_at DESC LIMIT 1"),
        {"t": ticker}).mappings().first()

    signal_asof = pick["recommendation_date"] if pick else today
    signals = [dict(r) for r in s.execute(text(
        "SELECT st.family, sig.rank, sig.formation_return, sig.valid_until "
        "FROM quant.signals sig "
        "JOIN market.instruments i ON i.id = sig.instrument_id "
        "JOIN quant.strategies st ON st.id = sig.strategy_id "
        "WHERE i.symbol = :t AND sig.valid_until >= :d "
        "ORDER BY sig.rank NULLS LAST"),
        {"t": ticker, "d": signal_asof}).mappings()]

    feats = (pick["features"] or {}) if pick else {}
    excess = ({h: (float(pick[f"excess_{h}"]) if pick[f"excess_{h}"] is not None else None)
               for h in (5, 10, 20, 60)} if pick
              else {5: None, 10: None, 20: None, 60: None})
    mom = feats.get("mom_12_1")
    if mom is None and models is not None:
        mom = (models.get("momentum") or {}).get("mom_12_1")

    atlas_rec = memo["recommendation"] if memo else None
    source_bullish = (pick["source_recommendation"] == "BUY") if pick else None
    cross = {
        "source_call": pick["source_recommendation"] if pick else None,
        "atlas_committee": atlas_rec or "not analyzed",
        "atlas_agrees": (None if (atlas_rec is None or source_bullish is None)
                         else (atlas_rec == "BUY") == source_bullish),
        "momentum_supports": (None if mom is None else mom > 0),
        "atlas_signal_flags": [sig["family"] for sig in signals],
        "outcome_so_far": ("not tracked" if not pick else (
            "too early" if excess[20] is None else
            ("outperforming" if excess[20] > 0 else
             "underperforming" if excess[20] < 0 else "flat"))),
    }
    return {
        "ticker": ticker,
        "source": pick["source"] if pick else None,
        "source_recommendation": pick["source_recommendation"] if pick else None,
        "recommendation_date": (pick["recommendation_date"].isoformat() if pick else None),
        "as_of_session": (pick["as_of_session"].isoformat() if pick else None),
        "features": feats, "excess": excess,
        "memo": (None if memo is None else {
            "recommendation": memo["recommendation"], "conviction": memo["conviction"],
            "thesis": memo["thesis"], "dissent": memo["dissent"],
            "kill_criteria": memo["kill_criteria"], "source": memo["source"],
            "created_at": memo["created_at"].isoformat()}),
        "atlas_signals": [{"family": sig["family"], "rank": sig["rank"],
                           "formation_return": (float(sig["formation_return"])
                                                if sig["formation_return"] is not None else None)}
                          for sig in signals],
        "models": models, "financials": financials, "valuation": valuation,
        "health": health, "autopsy": autopsy, "cross_check": cross,
    }


@router.get("/tickers/{symbol}/dossier")
def ticker_dossier(symbol: str) -> Any:
    """Full research dossier for ANY symbol — Atlas's own suggestions (committee
    memos, signals, proposals), not only external picks. Present-tense: models,
    financials, valuation and health are as of today. If the name also has a
    tracked external pick, its features and SPY-relative outcome enrich the view."""
    sym = symbol.upper()
    with session_scope() as s:
        iid = s.execute(text(
            "SELECT id FROM market.instruments WHERE symbol = :s "
            "ORDER BY is_active DESC LIMIT 1"), {"s": sym}).scalar()
        if iid is None:
            return JSONResponse(status_code=404, content={"error": {
                "code": "NOT_FOUND", "message": f"unknown symbol {sym}",
                "details": None}})
        pick = s.execute(text(
            "SELECT id, instrument_id, source, ticker, recommendation_date, "
            " as_of_session, source_recommendation, excess_5, excess_10, "
            " excess_20, excess_60, features FROM research.source_picks "
            "WHERE ticker = :s ORDER BY recommendation_date DESC LIMIT 1"),
            {"s": sym}).mappings().first()
        return _compose_dossier(s, iid, sym, pick=pick,
                                models_as_of=SystemClock().now().date())


@router.get("/source-picks/{pick_id}/dossier")
def source_pick_dossier(pick_id: str) -> Any:
    """One pick's full research dossier: the recommendation, its point-in-time
    fingerprint, performance vs SPY across horizons, Atlas's own committee memo
    (if the desk analyzed it), Atlas's independent signal-engine flags for the
    same name, and a CROSS-CHECK — where the source call, Atlas's view, the
    momentum model, and the actual outcome AGREE or DIVERGE. Divergence is where
    the learning lives. Strictly a read of what's recorded."""
    with session_scope() as s:
        pick = s.execute(text(
            "SELECT id, instrument_id, source, ticker, recommendation_date, "
            " as_of_session, source_recommendation, excess_5, excess_10, "
            " excess_20, excess_60, features FROM research.source_picks WHERE id = :id"),
            {"id": pick_id}).mappings().first()
        if pick is None:
            return JSONResponse(status_code=404, content={"error": {
                "code": "NOT_FOUND", "message": f"unknown pick {pick_id}",
                "details": None}})
        return _compose_dossier(s, pick["instrument_id"], pick["ticker"],
                                pick=pick, models_as_of=pick["as_of_session"])


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


DESK_SOURCE = "desk nightly"          # label for source IS NULL — the desk's own work
_CONVICTION_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "N/A": 3, "UNSPECIFIED": 4}


def _slice_rows(rec: str, graded: list[dict[str, Any]],
                memo_counts: list[dict[str, Any]], field: str,
                null_label: str) -> dict[str, dict[str, object]]:
    """One memo attribute (conviction | source) aggregated into scorecard
    sub-slices for a directional recommendation: memo count plus matured /
    vindicated / dissent-right counts per horizon. Keys are the attribute
    values verbatim, NULL relabelled (`null_label`); a slice with memos but
    no matured outcomes still appears — 0 matured is an honest answer."""
    def label(v: object) -> str:
        return null_label if v is None else str(v)

    keys = ({label(c[field]) for c in memo_counts if c["rec"] == rec}
            | {label(r[field]) for r in graded})
    order = ((lambda k: (_CONVICTION_ORDER.get(k, 9), k)) if field == "conviction"
             else (lambda k: (k != DESK_SOURCE, k)))
    out: dict[str, dict[str, object]] = {}
    for k in sorted(keys, key=order):
        rows_k = [r for r in graded if label(r[field]) == k]
        e: dict[str, object] = {"memos": sum(
            int(c["n"]) for c in memo_counts
            if c["rec"] == rec and label(c[field]) == k)}
        for h in (20, 60):
            hs = [r for r in rows_k if r["h"] == h]
            e[f"matured_{h}"] = len(hs)
            e[f"vindicated_{h}"] = sum(
                1 for r in hs
                if vindicated(rec, r["excess"], shadow=False) is True)
            e[f"dissent_right_{h}"] = sum(
                1 for r in hs
                if dissent_right(rec, r["excess"], shadow=False) is True)
        out[k] = e
    return out


@router.get("/scorecard")
def scorecard() -> dict[str, object]:
    """The desk graded against its own record (research.memo_outcomes,
    migration 0016; semantics in atlas/dcp/scorecard.py): SPY-relative per
    ADR-0009 — a BUY is vindicated when excess > 0 at the horizon, a REJECT
    when excess < 0 (the desk dodged an underperformer, even one that rose
    less than the market).

    by_recommendation carries BUY and REJECT only — the directional calls.
    Shadow-run memos are EXCLUDED from it (non-actionable, ADR-0005 pattern
    4) and surfaced honestly via shadow_excluded (matured outcome rows so
    excluded); HOLD and other non-directional recommendations are likewise
    excluded from the rates but appear in `recent` with vindicated=null.

    DESK-REVIEW 2026-07 ITEM 5 additions (all additive):
    - baseline_{h} / rate_minus_baseline_{h}: the dartboard base rate — what
      a direction-blind dart scores against ALL tracked outcomes at the
      horizon (HOLD and shadow included; dart_tracked carries the universe
      size) — and the slice's vindication rate minus it. This is the number
      that stops an always-REJECT desk in a falling market from looking
      smart: the dart matches it and the edge reads zero.
    - dissent_right_{h}: the dissent graded as the exact complement of
      vindicated() for directional memos (dead heats grade the dissent
      right); HOLD/shadow stay ungraded on both sides.
    - by_conviction / by_source sub-slices per recommendation (source NULL =
      'desk nightly' — the pick-filter loop verdict lives in by_source), and
      conviction / source / dissent_right on each `recent` row.
    Decimals cross to floats deliberately: display analytics, never ledger
    money."""
    with session_scope() as s:
        tracked = [dict(r) for r in s.execute(text(
            "SELECT o.horizon_sessions AS h, o.memo_id, o.fwd_return, "
            " o.spy_return, o.excess, m.instrument_symbol AS symbol, "
            " m.recommendation AS rec, m.conviction, m.source, "
            " COALESCE(ar.shadow, false) AS shadow "
            "FROM research.memo_outcomes o "
            "JOIN research.memos m ON m.id = o.memo_id "
            "LEFT JOIN research.agent_runs ar ON ar.id = m.agent_run_id "
            "ORDER BY o.computed_at DESC, o.memo_id, "
            " o.horizon_sessions")).mappings()]
        memo_counts = [dict(r) for r in s.execute(text(
            "SELECT m.recommendation AS rec, m.conviction, m.source, "
            " count(*) AS n "
            "FROM research.memos m "
            "LEFT JOIN research.agent_runs ar ON ar.id = m.agent_run_id "
            "WHERE m.memo_type = 'committee' "
            "  AND COALESCE(ar.shadow, false) = false "
            "GROUP BY 1, 2, 3")).mappings()]

    # the dart's universe: EVERY tracked outcome at the horizon — HOLD and
    # shadow rows included, because the dart is blind to what the desk said
    all_excess = {h: [r["excess"] for r in tracked if r["h"] == h]
                  for h in (20, 60)}

    by_rec: dict[str, dict[str, object]] = {}
    for rec in ("BUY", "REJECT"):
        graded = [r for r in tracked if r["rec"] == rec and not r["shadow"]]
        entry: dict[str, object] = {"memos": sum(
            int(c["n"]) for c in memo_counts if c["rec"] == rec)}
        for h in (20, 60):
            hs = [r for r in graded if r["h"] == h]
            wins = sum(1 for r in hs
                       if vindicated(rec, r["excess"], shadow=False) is True)
            entry[f"matured_{h}"] = len(hs)
            entry[f"vindicated_{h}"] = wins
            entry[f"avg_excess_{h}"] = (
                float(sum(r["excess"] for r in hs) / len(hs)) if hs else None)
            entry[f"dissent_right_{h}"] = sum(
                1 for r in hs
                if dissent_right(rec, r["excess"], shadow=False) is True)
            base = dartboard_baseline(rec, all_excess[h])
            entry[f"baseline_{h}"] = None if base is None else float(base)
            entry[f"rate_minus_baseline_{h}"] = (
                None if base is None or not hs
                else float(Decimal(wins) / Decimal(len(hs)) - base))
        entry["by_conviction"] = _slice_rows(rec, graded, memo_counts,
                                             "conviction", "UNSPECIFIED")
        entry["by_source"] = _slice_rows(rec, graded, memo_counts,
                                         "source", DESK_SOURCE)
        by_rec[rec] = entry

    recent = [
        {"memo_id": str(r["memo_id"]), "symbol": r["symbol"],
         "recommendation": r["rec"], "conviction": r["conviction"],
         "source": r["source"], "horizon": int(r["h"]),
         "fwd_return": float(r["fwd_return"]),
         "spy_return": float(r["spy_return"]),
         "excess": float(r["excess"]),
         "vindicated": vindicated(r["rec"], r["excess"],
                                  shadow=bool(r["shadow"])),
         "dissent_right": dissent_right(r["rec"], r["excess"],
                                        shadow=bool(r["shadow"]))}
        for r in tracked[:SCORECARD_RECENT]]

    return {"by_recommendation": by_rec, "recent": recent,
            "shadow_excluded": sum(1 for r in tracked if r["shadow"]),
            "dart_tracked": {"20": len(all_excess[20]),
                             "60": len(all_excess[60])}}


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
