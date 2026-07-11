"""Quant read surface (Doc 06): trial registry and validation verdicts.
Read-only — the API can never mutate quant state."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from atlas.core.db import session_scope

router = APIRouter()


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
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT id, strategy_id, verdict, reasons, created_at "
            "FROM quant.validation_reports ORDER BY created_at DESC LIMIT :n"),
            {"n": limit}).mappings()
        return [{**dict(r), "id": str(r["id"]), "strategy_id": str(r["strategy_id"]),
                 "created_at": r["created_at"].isoformat()} for r in rows]
