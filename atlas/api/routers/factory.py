"""Research Factory surface (phase 2 chassis): submit a pre-registered recipe
spec from the console, watch the gauntlet run, and browse every recipe verdict.

CONTRACT. POST /recipes/run is a TRIGGER, not a mutation (the analyze/screen
precedent): the API validates against the frozen v1 grammar and hands the spec
to the ops layer; every quant write happens inside the factory's own
registration-before-run discipline (atlas/dcp/factory/recipe_run), which this
surface invokes UNCHANGED by import. A refused spec is refused with the
grammar's own message verbatim — refusal, never coercion. The board and the
catalog are pure reads. Approval is a SEPARATE workflow: nothing on this
surface advances a recipe toward capital.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from atlas.core.db import session_scope
from atlas.dcp.factory.features import FEATURE_LINEAGE, RANKABLE_FEATURES
from atlas.dcp.factory.spec import (
    COST_BPS_PER_SIDE,
    TOP_N_MAX,
    TOP_N_MIN,
)

router = APIRouter()

_REPORTS = Path(__file__).resolve().parents[3] / "docs" / "reports"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
_KILL_FAMILY_SUFFIX = re.compile(r"-(19|20)\d{2}$")


class RecipeBody(BaseModel):
    spec: dict[str, Any] = Field(description="a v1-grammar recipe mapping")


@router.get("/recipes/catalog")
def recipes_catalog() -> dict[str, object]:
    """The closed v1 vocabulary, served from the source of truth so the console
    form can never offer an illegal choice: rankable features (with their BOUND
    lineage and pin), and the fixed grammar bounds. Widening any of this is a
    reviewed change in atlas/dcp/factory — never here."""
    # current per-lineage trial counts: the Principal sees the bar they are
    # about to raise BEFORE authorizing the burn
    with session_scope() as s:
        counts = {r.lineage: r.n for r in s.execute(text(
            "SELECT lineage, count(*) AS n FROM quant.trial_registry "
            "WHERE lineage IS NOT NULL GROUP BY lineage")).all()}
    feats = []
    for name, definition in sorted(RANKABLE_FEATURES.items()):
        lineage = FEATURE_LINEAGE[name]
        feats.append({
            "name": name,
            "lineage": lineage,
            "lineage_count": int(counts.get(lineage, 0)),
            "version": definition.version,
            "spec": dict(definition.spec),
        })
    return {
        "features": feats,
        "grammar": {
            "direction": ["desc"], "rebalance": ["monthly"],
            "universe": ["pit-sp500"],
            "cost_bps_per_side": COST_BPS_PER_SIDE,
            "top_n_min": TOP_N_MIN, "top_n_max": TOP_N_MAX,
        },
        "note": ("every run burns TWO counted trials (main + pre-committed "
                 "kill leg) against the feature's lineage — a hypothesis, "
                 "never a knob sweep"),
    }


@router.post("/recipes/run")
def recipes_run(body: RecipeBody) -> Any:
    """Validate against the frozen grammar and start the gauntlet in the
    background. Refusals come back 400 with the grammar's message verbatim;
    busy answers {started:false} honestly (one at a time)."""
    from atlas.ops.recipes import start_recipe

    out = start_recipe(body.spec)
    if out.get("refused"):
        return JSONResponse(status_code=400, content={"error": {
            "code": "SPEC_REFUSED", "message": str(out["refused"]),
            "details": None}})
    return out


@router.get("/recipes/status")
def recipes_status() -> dict[str, object]:
    """The current/last console-run recipe: running -> done|failed, with both
    legs' verdicts (and the demote-only strike rule applied in the wording)."""
    from atlas.ops.recipes import recipe_status

    return recipe_status()


@router.get("/recipes")
def recipes_board(limit: int = 50) -> list[dict[str, object]]:
    """The factory board: every registered recipe trial (console or CLI),
    newest first, each carrying its registered hypothesis and — where the leg
    completed — the gate verdict from the append-only audit event
    (quant.backtest.completed). A registered row with no completed event is
    reported as such (a crashed run's durable stub), never guessed."""
    limit = max(1, min(200, limit))
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT CAST(t.id AS text) AS trial_id, t.strategy_family AS family, "
            " t.lineage, t.hypothesis, t.spec_hash, t.created_at, "
            " e.payload AS ev "
            "FROM quant.trial_registry t "
            "LEFT JOIN LATERAL ("
            "  SELECT payload FROM audit.decision_events "
            "  WHERE event_type = 'quant.backtest.completed' "
            "    AND actor_id = 'recipe_run' "
            "    AND payload->>'trial_id' = CAST(t.id AS text) "
            "  ORDER BY created_at DESC LIMIT 1) e ON true "
            "WHERE t.strategy_family LIKE 'recipe-%' "
            "ORDER BY t.created_at DESC LIMIT :n"), {"n": limit}).mappings().all()
    out: list[dict[str, object]] = []
    for r in rows:
        family = r["family"]
        base = family.removeprefix("recipe-")
        is_kill = bool(_KILL_FAMILY_SUFFIX.search(base))
        ev = r["ev"] or {}
        out.append({
            "trial_id": r["trial_id"], "family": family,
            "name": (_KILL_FAMILY_SUFFIX.sub("", base) if is_kill else base),
            "leg": "kill" if is_kill else "main",
            "lineage": r["lineage"], "hypothesis": r["hypothesis"],
            "spec_hash": r["spec_hash"],
            "created_at": r["created_at"].isoformat(),
            "completed": bool(ev),
            "gate_passed": ev.get("gate_passed"),
            "gate_reasons": ev.get("gate_reasons"),
            "dsr": ev.get("dsr"), "null_p": ev.get("null_p"),
            "n_trials": ev.get("n_trials"),
        })
    return out


@router.get("/recipes/{name}/report")
def recipe_report(name: str) -> Any:
    """The persisted gauntlet report (markdown) for one recipe name, served so
    the console can render it without filesystem access. 404 for an unknown or
    grammar-illegal name (the name is also the path component — validate it)."""
    if not _NAME_RE.fullmatch(name):
        return JSONResponse(status_code=400, content={"error": {
            "code": "INVALID_NAME", "message": "not a legal recipe name",
            "details": None}})
    path = _REPORTS / f"recipe-{name}.md"
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": {
            "code": "NOT_FOUND", "message": f"no report for recipe {name}",
            "details": None}})
    return PlainTextResponse(path.read_text())
