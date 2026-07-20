"""Quant read surface (Doc 06): trial registry, validation verdicts, and the
real-data gate report. Read-only — the API can never mutate quant state."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import SystemClock
from atlas.core.db import session_scope
from atlas.dcp.signals.xsmom.generate import next_rebalance_session
from atlas.dcp.strategy_lifecycle import is_authoritative

router = APIRouter()

_REPORT = Path(__file__).resolve().parents[2].parent / "docs" / "reports" / \
    "decision-grade-momentum-v1.md"
# the gauntlet's standing thresholds (Doc 08 / dcp.backtest.approval)
_NULL_P_MAX = 0.05
_DSR_MIN = 0.90
_WF_TOTAL = 4
_LIVE_STATES = ("paper", "live", "suspended")
# Display surface (ADR-0018): research_shadow is SHOWN but never as validated —
# so a downgraded strategy is visibly labelled, never silently hidden.
_DISPLAY_STATES = ("paper", "live", "suspended", "research_shadow")


def _validation_label(state: str | None) -> dict[str, object]:
    """ADR-0018 non-authoritative label: a research_shadow (or otherwise
    non-paper/live) strategy is presented but never counted as validated
    performance — the console renders `authoritative=false` distinctly."""
    return {
        "authoritative": is_authoritative(state),
        "validation_status": ("validated" if is_authoritative(state)
                              else (state or "unknown")),
    }


def _f(pattern: str, text_: str) -> float | None:
    m = re.search(pattern, text_)
    return float(m.group(1)) if m else None


def _momentum_v1_graveyard() -> dict[str, object] | None:
    """Momentum v1's real-data FAILURE, parsed verbatim from the checkpoint-3
    report — kept as an honest GRAVEYARD entry (not the live record). None if
    the report file is absent."""
    if not _REPORT.exists():
        return None
    raw = _REPORT.read_text()
    out = []
    for m in re.finditer(r"## (\w+) — (.*?)(?=\n## |\Z)", raw, re.S):
        sym, body = m.group(1), m.group(2)
        folds = re.search(r"fold returns: (.+)", body)
        out.append({
            "symbol": sym,
            "window": body.split("(")[0].strip(),
            "verdict": "FAIL" if "**FAIL**" in body else
                       "PASS" if "**PASS**" in body else "—",
            "strategy_return_pct": _f(r"strategy return: ([+-]?\d+\.\d+)%", body),
            "bh_return_pct": _f(r"buy-and-hold return: ([+-]?\d+\.\d+)%", body),
            "null_p": _f(r"null-model p-value: (\d+\.\d+)", body),
            "dsr": _f(r"deflated Sharpe: (\d+\.\d+)", body),
            "wf_positive": _f(r"Walk-forward: (\d+)/", body),
            "wf_total": _f(r"Walk-forward: \d+/(\d+)", body),
            "fold_returns": folds.group(1).strip() if folds else None,
            "sharpe": _f(r"Sharpe ([+-]?\d+\.\d+)", body),
            "max_drawdown_pct": _f(r"max drawdown ([+-]?\d+\.\d+)%", body),
            "trial_id": (re.search(r"`([0-9a-f-]{36})`", body) or [None, None])[1],
        })
    return {"strategy": "momentum v1",
            "warning": "ADR-0004: one-year window — indicative only, NOT decision-grade",
            "symbols": out}


def _live_gate_verdicts(s: Session) -> list[dict[str, object]]:
    """The gate record for the CURRENTLY LIVE strategies (paper/live/suspended):
    each strategy's latest validation checklist — the real, Principal-signed
    PASSes on regenerated real-data artifacts."""
    rows = s.execute(text(
        "SELECT DISTINCT ON (st.id) st.name, st.family, st.state, st.approved_by, "
        "       st.approved_at, vr.verdict, vr.checklist "
        "FROM quant.strategies st "
        "JOIN quant.validation_reports vr ON vr.strategy_id = st.id "
        "WHERE st.state IN ('paper','live','suspended','research_shadow') "
        "ORDER BY st.id, vr.created_at DESC")).mappings().all()
    out: list[dict[str, object]] = []
    for r in rows:
        ck = r["checklist"] if isinstance(r["checklist"], dict) else {}
        passed = bool(ck.get("gate_passed")) or r["verdict"] == "approve"
        out.append({
            "strategy": r["family"], "name": r["name"], "state": r["state"],
            **_validation_label(r["state"]),
            "approved_by": r["approved_by"],
            "approved_at": r["approved_at"].isoformat() if r["approved_at"] else None,
            "verdict": "PASS" if passed else "FAIL",
            "null_p": ck.get("null_p"), "null_p_max": _NULL_P_MAX,
            "dsr": ck.get("dsr"), "dsr_min": _DSR_MIN,
            "wf_positive": ck.get("wf_positive_folds"), "wf_total": _WF_TOTAL,
            "n_trials": ck.get("n_trials_family"),
            "decision_ref": ck.get("decision_ref"),
            "trial_id": ck.get("trial_id"), "report": ck.get("report"),
        })
    out.sort(key=lambda x: str(x["approved_at"] or ""), reverse=True)
    return out


@router.get("/gate-report")
def gate_report() -> dict[str, object]:
    """The real-data gate record: the LIVE approved strategies' validation
    checklists (the signed PASSes) as the primary record, plus momentum v1's
    failure kept as a graveyard entry (honest failures are deliverables)."""
    with session_scope() as s:
        live = _live_gate_verdicts(s)
    return {"available": True, "live": live, "graveyard": _momentum_v1_graveyard()}


@router.get("/strategies")
def strategies() -> list[dict[str, object]]:
    """Approved-strategy register (ADR-0010): the strategy row, its live
    signal footprint, and the latest band-check reading — read-only; the
    console's STRATEGY card renders this verbatim. Excess reads 'dormant'
    until 126 sleeve sessions exist (the band is a 126-session statistic)."""
    today = SystemClock().now().date()
    out: list[dict[str, object]] = []
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT id, family, name, version, state, approved_by, approved_at, "
            "       tolerance_bands "
            "FROM quant.strategies "
            "WHERE state IN ('paper','live','suspended','research_shadow') "
            "ORDER BY family, created_at")).mappings().all()
        for r in rows:
            n_signals = s.execute(text(
                "SELECT count(*) FROM quant.signals "
                "WHERE strategy_id = :sid AND valid_until >= :d"),
                {"sid": r["id"], "d": today}).scalar()
            band = s.execute(text(
                "SELECT session_date, sleeve_value, drawdown, excess_126s_pp "
                "FROM quant.sleeve_daily WHERE strategy_id = :sid "
                "ORDER BY session_date DESC LIMIT 1"),
                {"sid": r["id"]}).mappings().first()
            out.append({
                "id": str(r["id"]), "family": r["family"], "name": r["name"],
                "version": r["version"], "state": r["state"],
                **_validation_label(r["state"]),
                "approved_by": r["approved_by"],
                "approved_at": (r["approved_at"].isoformat()
                                if r["approved_at"] else None),
                "tolerance_bands": r["tolerance_bands"],
                "active_signals": int(n_signals or 0),
                "next_rebalance": next_rebalance_session(today).isoformat(),
                "band_status": None if band is None else {
                    "session_date": band["session_date"].isoformat(),
                    "sleeve_value": (None if band["sleeve_value"] is None
                                     else float(band["sleeve_value"])),
                    "drawdown": (None if band["drawdown"] is None
                                 else float(band["drawdown"])),
                    "excess_126s_pp": (None if band["excess_126s_pp"] is None
                                       else float(band["excess_126s_pp"])),
                }})
    return out


@router.get("/trials")
def trials(family: str | None = None, limit: int = 50) -> list[dict[str, object]]:
    q = ("SELECT id, strategy_family, lineage, spec_hash, metrics, created_at "
         "FROM quant.trial_registry")
    params: dict[str, object] = {"n": limit}
    if family:
        q += " WHERE strategy_family = :f"
        params["f"] = family
    q += " ORDER BY created_at DESC LIMIT :n"
    with session_scope() as s:
        rows = s.execute(text(q), params).mappings()
        return [{**dict(r), "id": str(r["id"]),
                 "created_at": r["created_at"].isoformat()} for r in rows]


@router.get("/verdicts")
def verdicts(limit: int = 50) -> list[dict[str, object]]:
    """Approval decisions (change control). Joined to the strategy row so the
    console can show PROVENANCE per row: a live/paper strategy signed by the
    Principal is a real-data approval; anything else is synthetic-fixture-era —
    a synthetic pass must never render as a real one. `real_signed` is the honest
    flag (state is live AND a signer is recorded); `decision_ref` comes from the
    validation checklist."""
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT vr.id, vr.strategy_id, vr.verdict, vr.reasons, vr.created_at, "
            "       vr.checklist, s.name AS strategy_name, s.state AS strategy_state, "
            "       s.approved_by "
            "FROM quant.validation_reports vr "
            "LEFT JOIN quant.strategies s ON s.id = vr.strategy_id "
            "ORDER BY vr.created_at DESC LIMIT :n"),
            {"n": limit}).mappings()
        out: list[dict[str, object]] = []
        for r in rows:
            ck = r["checklist"] if isinstance(r["checklist"], dict) else {}
            out.append({
                "id": str(r["id"]), "strategy_id": str(r["strategy_id"]),
                "verdict": r["verdict"], "reasons": r["reasons"],
                "created_at": r["created_at"].isoformat(),
                "strategy_name": r["strategy_name"],
                "strategy_state": r["strategy_state"],
                **_validation_label(r["strategy_state"]),
                "approved_by": r["approved_by"],
                "decision_ref": ck.get("decision_ref"),
                "real_signed": (r["strategy_state"] in _LIVE_STATES
                                and bool(r["approved_by"])),
            })
        return out
