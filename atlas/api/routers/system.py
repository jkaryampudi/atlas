from __future__ import annotations

from fastapi import APIRouter

from atlas.core.config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict[str, object]:
    s = get_settings()
    return {
        "status": "ok",
        "trading_mode": s.trading_mode,
        "armed": False,  # live arming is a Phase 6 mechanism; always false until then
        "limit_mode": s.limit_mode,
        "base_currency": s.base_currency,
    }


@router.get("/mode")
def mode() -> dict[str, object]:
    s = get_settings()
    return {"trading_mode": s.trading_mode, "armed": False}


@router.get("/pipeline-runs")
def pipeline_runs(limit: int = 14) -> list[dict[str, object]]:
    """The ops jobs board (roundtable item 7): every workflow run — the
    T0-T9 daily cycles, backfills, replays — with its per-node results, so
    'did last night run, and what did each step say' is a glance, not a
    log dive. Node output_ref strings are the steps' own one-line reports
    (bars=…, fills=…, nav=…)."""
    from sqlalchemy import text

    from atlas.core.db import session_scope
    with session_scope() as s:
        runs = s.execute(text(
            "SELECT run_id, status, started_at, completed_at "
            "FROM workflow.workflow_runs ORDER BY started_at DESC LIMIT :n"),
            {"n": limit}).mappings().all()
        ids = [r["run_id"] for r in runs]
        nodes: dict[str, list[dict[str, object]]] = {i: [] for i in ids}
        if ids:
            for nr in s.execute(text(
                    "SELECT run_id, node_name, status, output_ref, completed_at "
                    "FROM workflow.workflow_node_results WHERE run_id = ANY(:ids) "
                    "ORDER BY completed_at, node_name"), {"ids": ids}).mappings():
                nodes[nr["run_id"]].append({
                    "node": nr["node_name"], "status": nr["status"],
                    "result": nr["output_ref"],
                    "completed_at": nr["completed_at"].isoformat()})
        return [{"run_id": r["run_id"], "status": r["status"],
                 "started_at": r["started_at"].isoformat(),
                 "completed_at": r["completed_at"].isoformat()
                 if r["completed_at"] else None,
                 "nodes": nodes[r["run_id"]]} for r in runs]
