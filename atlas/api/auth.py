"""API authentication for state-mutating endpoints (F-016).

The console API had NO authentication: any local process could approve proposals,
cancel orders or close positions — the loopback bind was the only control. This
module adds a fail-closed bearer-token dependency for the mutating endpoints.

Posture (deny by default):
  * the expected token comes ONLY from the ATLAS_API_TOKEN environment variable —
    there is NO default/baked-in credential;
  * if ATLAS_API_TOKEN is unset, mutating endpoints are DISABLED (503) — a
    misconfigured deployment cannot mutate state, rather than silently allowing
    everyone;
  * a missing/malformed Authorization header -> 401; a well-formed but wrong
    token -> 403; comparison is constant-time.

Read-only endpoints and the console page remain open (loopback-bound, no state
change). The token never enters logs or audit payloads.
"""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException

_SCHEME = "Bearer "


def _expected_token() -> str | None:
    tok = os.environ.get("ATLAS_API_TOKEN")
    return tok if tok else None


def require_api_auth(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: authorise a state-mutating request or fail closed."""
    expected = _expected_token()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ATLAS_API_TOKEN is not configured — state-mutating endpoints "
                   "are disabled (fail closed).")
    if not authorization or not authorization.startswith(_SCHEME):
        raise HTTPException(status_code=401, detail="missing or malformed bearer token")
    presented = authorization[len(_SCHEME):]
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="invalid token")
