"""Shared dashboard API client — the dashboard is a PURE API client (Doc 06 §6):
no database access, no imports from atlas.dcp; every panel degrades independently."""
from __future__ import annotations

import os

import httpx

API = os.environ.get("ATLAS_API_URL", "http://localhost:8001")


def get_json(path: str) -> tuple[object | None, str | None]:
    try:
        r = httpx.get(f"{API}{path}", timeout=5)
        if r.status_code == 200:
            return r.json(), None
        return None, f"{r.status_code} from {path}"
    except httpx.HTTPError as e:
        return None, f"{path}: {type(e).__name__}"
