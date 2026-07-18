"""Reporting surface (ops-reliability build, 2026-07): the morning brief.

GET /v1/reporting/brief/latest serves the newest persisted
reporting.morning_brief row VERBATIM — the brief is assembled and persisted
by the daily cycle's t9b node (atlas/dcp/reporting/brief.py); this layer
never assembles, never recomputes, never freshens. A 404 is an honest state:
no cycle has persisted a brief yet.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from atlas.core.db import session_scope
from atlas.dcp.reporting.brief import latest_brief

router = APIRouter()


@router.get("/brief/latest")
def brief_latest() -> dict[str, Any]:
    with session_scope() as s:
        row = latest_brief(s)
    if row is None:
        raise HTTPException(
            404, "no morning brief persisted yet — the daily cycle "
                 "assembles one after t9 (node t9b)")
    return row
