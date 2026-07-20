"""The Principal's morning brief — assembled, persisted, never computed.

Ops-reliability build, 2026-07. The observed failures this fixes: core
proposals silently expired unapproved (twice), four API-billing outages in
five days produced $0.00 desk nights that nothing surfaced, and the morning
picture lived in six console panels and a log file. After t9 the daily cycle
(t9b) calls persist_brief(): one deterministic jsonb document per session in
reporting.morning_brief (migration 0031), served by
GET /v1/reporting/brief/latest and rendered as the BRIEF card at the top of
the console Trading page.

ASSEMBLY, NOT COMPUTATION — every field is a read of rows other code already
wrote this cycle:
  * cycle node results   workflow.workflow_node_results (run daily-<date>),
                         including failures: a 'failed' status is a hard
                         node death; an output_ref containing 'FAILED' is the
                         fail-soft convention every tN node uses
  * queue + countdowns   trading.trade_proposals in pending_approval, each
                         with hours_left against the injected clock and an
                         expiring_soon flag at the alerting threshold
  * memos + verdicts     research.memos for the session, joined to any
                         proposal they bridged into
  * attribution          the latest stored reporting.attribution_daily rows
  * bands / CUSUM        quant.strategies + latest quant.sleeve_daily row,
                         plus the demotion/CUSUM audit events
  * learning one-liner   extracted from the t9 node line (the learning
                         modules are READ via their recorded summary, never
                         re-run here)
  * budget               research.agent_runs spend vs the global cap and the
                         nightly watermark
  * urgent alerts        today's ops.alert.urgent audit events — present in
                         the brief even when ATLAS_ALERT_URL is unset, which
                         is exactly the point (atlas/ops/alerts.py)

The brief PROMINENTLY flags (payload["flags"]): proposals expiring within
EXPIRING_SOON, any FAILED node, any band/CUSUM event today, and the
billing-outage signature — desk spend $0.00 with zero memos on a night the
desk was expected to produce.

Idempotent by construction: one UNIQUE(session_date) row, upserted; the same
database state and clock instant assemble byte-identical payloads.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.core.config import get_settings
from atlas.dcp.reporting.attribution import (
    satellite_sleeve_meta,
    scoped_performance,
)
from atlas.dcp.strategy_lifecycle import (
    AUTHORITATIVE_PORTFOLIO,
    RESEARCH_SHADOW,
    classify,
)
from atlas.ops.alerts import EXPIRING_SOON, URGENT_EVENT

BRIEF_EVENT = "reporting.brief.assembled"
# The nightly watermark default mirrors runner.SURFACE_BUDGET_DEFAULTS_USD
# ["nightly"] — read via the same env var, duplicated as a constant because
# the two-plane wall forbids dcp importing atlas.agents. runner.py is the
# source of truth; a change there is a reviewed change here.
NIGHTLY_WATERMARK_DEFAULT_USD = 6.00

# Fail-soft node lines all embed this marker ("desk FAILED: ...",
# "bands FAILED: ...", "scan FAILED (...)"): the honest-failure convention
# the daily cycle established (atlas/ops/daily.py).
_FAILSOFT_MARKER = "FAILED"


@dataclass(frozen=True)
class MorningBrief:
    session_date: date
    payload: dict[str, Any]

    def summary(self) -> str:
        q = self.payload["queue"]
        flags = self.payload["flags"]
        line = (f"brief {self.session_date.isoformat()}: "
                f"queue {len(q['proposals'])} "
                f"({q['expiring_soon_count']} expiring soon) · "
                f"failed nodes {len(flags['failed_nodes'])} · "
                f"memos {len(self.payload['memos'])} · "
                f"spend ${self.payload['budget']['spend_usd']:.2f}")
        if flags["billing_outage_suspected"]:
            line += " · BILLING-OUTAGE SIGNATURE"
        return line


def _cycle_block(session: Session, run_id: str) -> dict[str, Any]:
    run = session.execute(text(
        "SELECT status FROM workflow.workflow_runs WHERE run_id = :r"),
        {"r": run_id}).first()
    rows = session.execute(text(
        "SELECT node_name, status, output_ref "
        "FROM workflow.workflow_node_results WHERE run_id = :r "
        "ORDER BY completed_at, node_name"), {"r": run_id}).all()
    nodes = [{"node": r.node_name, "status": r.status,
              "result": r.output_ref} for r in rows]
    # three failure spellings, all honest: a hard node death ('failed'
    # status), the fail-soft '... FAILED: ...' line convention, and t0's
    # 'failed=True' ingest flag (the same trio main() folds into the exit
    # code — atlas/ops/daily.py).
    failed = [n["node"] for n in nodes
              if n["status"] == "failed"
              or _FAILSOFT_MARKER in str(n["result"] or "")
              or "failed=True" in str(n["result"] or "")]
    return {"run_id": run_id,
            "run_status": run.status if run is not None else "missing",
            "nodes": nodes, "failed_nodes": failed}


def _queue_block(session: Session, clock: Clock) -> dict[str, Any]:
    now = clock.now()
    soon_h = EXPIRING_SOON.total_seconds() / 3600
    rows = session.execute(text(
        "SELECT tp.id, tp.action, tp.origin, tp.position_size, "
        "       tp.position_value_aud, tp.expires_at, tp.created_at, i.symbol "
        "FROM trading.trade_proposals tp "
        "LEFT JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE tp.state = 'pending_approval' ORDER BY tp.expires_at, tp.id")).all()
    proposals: list[dict[str, Any]] = []
    for r in rows:
        hours_left = round(max(
            0.0, (r.expires_at - now).total_seconds() / 3600), 1)
        proposals.append({
            "id": str(r.id), "symbol": r.symbol, "action": r.action,
            "origin": r.origin, "qty": int(r.position_size),
            "value_aud": str(r.position_value_aud),
            "expires_at": r.expires_at.isoformat(),
            "hours_left": hours_left,
            "expiring_soon": hours_left <= soon_h})
    return {"proposals": proposals,
            "expiring_soon_count": sum(1 for p in proposals
                                       if p["expiring_soon"])}


def _memo_block(session: Session, on: date) -> list[dict[str, Any]]:
    rows = session.execute(text(
        "SELECT m.id, m.memo_type, m.instrument_symbol, m.recommendation, "
        "       m.conviction, m.source, "
        "       (SELECT tp.state FROM trading.trade_proposals tp "
        "        WHERE tp.committee_memo_id = m.id "
        "        ORDER BY tp.created_at DESC LIMIT 1) AS proposal_state "
        "FROM research.memos m WHERE m.created_at::date = :d "
        "ORDER BY m.created_at, m.id"), {"d": on}).all()
    return [{"id": str(r.id), "memo_type": r.memo_type,
             "symbol": r.instrument_symbol, "recommendation": r.recommendation,
             "conviction": r.conviction, "source": r.source,
             "proposal_state": r.proposal_state} for r in rows]


def _attribution_block(session: Session) -> dict[str, Any] | None:
    last = session.execute(text(
        "SELECT max(session_date) FROM reporting.attribution_daily")).scalar()
    if last is None:
        return None
    rows = session.execute(text(
        "SELECT sleeve, value_aud, ret_1d, benchmark_ret_1d "
        "FROM reporting.attribution_daily WHERE session_date = :d "
        "ORDER BY sleeve"), {"d": last}).all()
    sleeves = {r.sleeve: {
        "value_aud": str(r.value_aud),
        "ret_1d": None if r.ret_1d is None else float(r.ret_1d),
        "benchmark_ret_1d": (None if r.benchmark_ret_1d is None
                             else float(r.benchmark_ret_1d))} for r in rows}

    def _cell(name: str) -> str:
        s = sleeves.get(name)
        return "n/a" if s is None else f"A${s['value_aud']}"

    line = (f"attribution {last.isoformat()}: "
            + " · ".join(f"{n} {_cell(n)}"
                         for n in ("core", "xsmom", "pead", "cash", "total")))
    # ADR-0018: the brief's performance number is the AUTHORITATIVE composite
    # (shadow sleeves excluded); the raw per-sleeve values above stay for
    # transparency, and any shadow sleeve is named so the console can label it.
    perf = scoped_performance(session, AUTHORITATIVE_PORTFOLIO)
    meta = satellite_sleeve_meta(session)
    shadow_sleeves = sorted(sv for sv, m in meta.items()
                            if classify(m["state"]) == RESEARCH_SHADOW)
    alpha = perf["satellite_alpha_pp"]
    return {"session_date": last.isoformat(), "sleeves": sleeves, "line": line,
            "performance_scope": perf["performance_scope"],
            "authoritative": perf["authoritative"],
            "satellite_alpha_pp": float(alpha) if isinstance(alpha, Decimal) else None,
            "contains_shadow_results": perf["contains_shadow_results"],
            "caveat": perf["caveat"],
            "shadow_sleeves": shadow_sleeves}


def _strategy_block(session: Session, on: date) -> list[dict[str, Any]]:
    rows = session.execute(text(
        "SELECT s.id, s.family, s.state, "
        "  sd.session_date, sd.sleeve_value, sd.drawdown, sd.excess_126s_pp, "
        "  EXISTS (SELECT 1 FROM audit.decision_events e "
        "          WHERE e.event_type = 'quant.strategy.cusum_breach' "
        "            AND e.entity_id = s.id::text) AS cusum_breached, "
        "  EXISTS (SELECT 1 FROM audit.decision_events e "
        "          WHERE e.event_type = 'quant.strategy.demoted' "
        "            AND e.entity_id = s.id::text "
        "            AND e.created_at::date = :d) AS demoted_today "
        "FROM quant.strategies s "
        "LEFT JOIN LATERAL (SELECT * FROM quant.sleeve_daily x "
        "                   WHERE x.strategy_id = s.id "
        "                   ORDER BY x.session_date DESC LIMIT 1) sd ON true "
        "WHERE s.state IN ('paper','live','suspended') "
        "ORDER BY s.family, s.created_at"), {"d": on}).all()
    return [{"family": r.family, "state": r.state,
             "session": None if r.session_date is None
             else r.session_date.isoformat(),
             "sleeve_value": None if r.sleeve_value is None
             else str(r.sleeve_value),
             "drawdown": None if r.drawdown is None else float(r.drawdown),
             "excess_126s_pp": None if r.excess_126s_pp is None
             else float(r.excess_126s_pp),
             "cusum_breached": bool(r.cusum_breached),
             "demoted_today": bool(r.demoted_today)} for r in rows]


def _learning_line(cycle: dict[str, Any]) -> str:
    """The learning one-liner as t9 recorded it (' · '-joined segments; the
    learning segment always begins with 'learning') — read, never re-run."""
    for node in cycle["nodes"]:
        if node["node"] == "t9_report":
            for seg in str(node["result"] or "").split(" · "):
                if seg.startswith("learning"):
                    return seg
    return "learning: no t9 report this session"


def _budget_block(session: Session, on: date) -> dict[str, Any]:
    spend = session.execute(text(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM research.agent_runs "
        "WHERE created_at::date = :d"), {"d": on}).scalar()
    by_status = {r.status: int(r.n) for r in session.execute(text(
        "SELECT status, count(*) AS n FROM research.agent_runs "
        "WHERE created_at::date = :d GROUP BY status ORDER BY status"),
        {"d": on}).all()}
    nightly = float(os.environ.get("ATLAS_BUDGET_NIGHTLY",
                                   NIGHTLY_WATERMARK_DEFAULT_USD))
    return {"spend_usd": round(float(spend or 0), 4),
            "daily_cap_usd": float(get_settings().daily_llm_budget_usd),
            "nightly_watermark_usd": nightly,
            "runs_by_status": by_status}


def _desk_block(cycle: dict[str, Any], memos: list[dict[str, Any]],
                budget: dict[str, Any]) -> dict[str, Any]:
    line = next((str(n["result"] or "") for n in cycle["nodes"]
                 if n["node"] == "t7_desk"), None)
    expected = (line is not None
                and not line.startswith("desk off")
                and not line.startswith("desk skipped"))
    failed = line is not None and _FAILSOFT_MARKER in line
    committee = sum(1 for m in memos if m["memo_type"] == "committee")
    return {"line": line, "expected": expected, "failed": failed,
            "memo_count": committee, "spend_usd": budget["spend_usd"]}


def _urgent_alerts(session: Session, on: date) -> list[dict[str, Any]]:
    rows = session.execute(text(
        "SELECT entity_id, payload, created_at FROM audit.decision_events "
        "WHERE event_type = :et AND created_at::date = :d ORDER BY seq"),
        {"et": URGENT_EVENT, "d": on}).all()
    return [{"key": r.entity_id, "kind": r.payload.get("kind"),
             "title": r.payload.get("title"),
             "priority": r.payload.get("priority"),
             "delivered": r.payload.get("delivered"),
             "at": r.created_at.isoformat()} for r in rows]


def assemble_brief(session: Session, clock: Clock) -> MorningBrief:
    """Assemble (never compute) the session's brief — module docstring has
    the field-by-field provenance. Pure read; deterministic for a given
    database state and clock instant."""
    now = clock.now()
    on = now.date()
    cycle = _cycle_block(session, f"daily-{on.isoformat()}")
    queue = _queue_block(session, clock)
    memos = _memo_block(session, on)
    budget = _budget_block(session, on)
    desk = _desk_block(cycle, memos, budget)
    strategies = _strategy_block(session, on)
    alerts = _urgent_alerts(session, on)

    band_events = [s["family"] for s in strategies
                   if s["demoted_today"] or s["cusum_breached"]]
    billing_alerted = any(a["kind"] == "billing_outage" for a in alerts)
    # THE BILLING-OUTAGE SIGNATURE (module docstring): a desk that was
    # expected to produce, spent $0.00 and memo'd nothing — or the detector
    # already paged it (atlas/ops/alerts.py maybe_billing_outage_alert).
    billing_suspected = billing_alerted or (
        desk["expected"] and desk["spend_usd"] == 0.0
        and desk["memo_count"] == 0)

    payload: dict[str, Any] = {
        "session_date": on.isoformat(),
        "generated_at": now.isoformat(),
        "cycle": cycle,
        "queue": queue,
        "memos": memos,
        "attribution": _attribution_block(session),
        "strategies": strategies,
        "learning_line": _learning_line(cycle),
        "budget": budget,
        "desk": desk,
        "urgent_alerts": alerts,
        "flags": {
            "no_cycle_run": cycle["run_status"] == "missing",
            "failed_nodes": cycle["failed_nodes"],
            "expiring_proposals": [p["id"] for p in queue["proposals"]
                                   if p["expiring_soon"]],
            "band_or_cusum_events": band_events,
            "billing_outage_suspected": billing_suspected,
        },
    }
    return MorningBrief(session_date=on, payload=payload)


def persist_brief(session: Session, clock: Clock) -> MorningBrief:
    """Assemble + upsert the one row per session (idempotent re-assembly
    replaces the payload in place) + one audit event per assembly."""
    brief = assemble_brief(session, clock)
    now = clock.now()
    session.execute(text(
        "INSERT INTO reporting.morning_brief "
        "(session_date, payload, created_at, updated_at) "
        "VALUES (:d, CAST(:p AS jsonb), :t, :t) "
        "ON CONFLICT (session_date) DO UPDATE SET "
        "  payload = CAST(:p AS jsonb), updated_at = :t"),
        {"d": brief.session_date, "p": json.dumps(brief.payload), "t": now})
    flags = brief.payload["flags"]
    PostgresAuditLog(session, clock).append(
        event_type=BRIEF_EVENT, entity_type="morning_brief",
        entity_id=brief.session_date.isoformat(), actor_type="dcp",
        actor_id="morning_brief",
        payload={"session_date": brief.session_date.isoformat(),
                 "queue": len(brief.payload["queue"]["proposals"]),
                 "expiring_soon":
                     brief.payload["queue"]["expiring_soon_count"],
                 "failed_nodes": flags["failed_nodes"],
                 "band_or_cusum_events": flags["band_or_cusum_events"],
                 "billing_outage_suspected":
                     flags["billing_outage_suspected"],
                 "summary": brief.summary()})
    return brief


def latest_brief(session: Session) -> dict[str, Any] | None:
    """The API read (GET /v1/reporting/brief/latest): newest persisted brief
    row, payload verbatim — the console renders from this document alone."""
    row = session.execute(text(
        "SELECT session_date, payload, created_at, updated_at "
        "FROM reporting.morning_brief ORDER BY session_date DESC LIMIT 1")
    ).first()
    if row is None:
        return None
    return {"session_date": row.session_date.isoformat(),
            "payload": row.payload,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat()}
