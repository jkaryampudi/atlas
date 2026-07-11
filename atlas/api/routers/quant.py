"""Quant read surface (Doc 06): trial registry, validation verdicts, and the
real-data gate report. Read-only — the API can never mutate quant state."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import text

from atlas.core.db import session_scope

router = APIRouter()

_REPORT = Path(__file__).resolve().parents[2].parent / "docs" / "reports" / \
    "first-real-backtest-momentum-v1.md"


def _f(pattern: str, text_: str) -> float | None:
    m = re.search(pattern, text_)
    return float(m.group(1)) if m else None


@router.get("/gate-report")
def gate_report() -> dict[str, object]:
    """The written real-data gate verdicts (checkpoint 3), parsed per symbol.
    This is the honest quant record: momentum v1 FAILED — reported verbatim."""
    if not _REPORT.exists():
        return {"available": False, "symbols": []}
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
            "null_p_max": 0.05,
            "dsr": _f(r"deflated Sharpe: (\d+\.\d+)", body),
            "dsr_min": 0.90,
            "n_trials": _f(r"n_trials=(\d+)", body),
            "wf_positive": _f(r"Walk-forward: (\d+)/", body),
            "wf_total": _f(r"Walk-forward: \d+/(\d+)", body),
            "fold_returns": folds.group(1).strip() if folds else None,
            "sharpe": _f(r"Sharpe ([+-]?\d+\.\d+)", body),
            "max_drawdown_pct": _f(r"max drawdown ([+-]?\d+\.\d+)%", body),
            "trial_id": (re.search(r"`([0-9a-f-]{36})`", body) or [None, None])[1],
        })
    return {"available": True, "strategy": "momentum v1",
            "warning": "ADR-0004: one-year window — indicative only, NOT decision-grade",
            "symbols": out}


@router.get("/trials")
def trials(family: str | None = None, limit: int = 50) -> list[dict[str, object]]:
    q = ("SELECT id, strategy_family, spec_hash, metrics, created_at "
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
    console can show provenance — a synthetic-fixture-era approval must never
    render as a real-data pass."""
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT vr.id, vr.strategy_id, vr.verdict, vr.reasons, vr.created_at, "
            "       s.name AS strategy_name, s.state AS strategy_state "
            "FROM quant.validation_reports vr "
            "LEFT JOIN quant.strategies s ON s.id = vr.strategy_id "
            "ORDER BY vr.created_at DESC LIMIT :n"),
            {"n": limit}).mappings()
        return [{**dict(r), "id": str(r["id"]), "strategy_id": str(r["strategy_id"]),
                 "created_at": r["created_at"].isoformat()} for r in rows]
