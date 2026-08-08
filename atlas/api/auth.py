"""Central, deny-by-default API authentication + authorisation (F-016).

Every non-public API route requires a bearer token; every STATE-MUTATING route
additionally requires the route's classified ROLE. A mutating route that carries
no auth dependency fails the conformance test (`test_api_auth` /
`test_route_security_inventory`) — deny by default, so a newly-added unclassified
mutator cannot ship open.

Tokens -> roles (env only; NO baked-in default):
  * ``ATLAS_API_TOKEN`` — the admin token: grants EVERY role (the single-Principal
    paper deployment, where one operator holds all duties).
  * ``ATLAS_API_TOKEN_<ROLE>`` (e.g. ``ATLAS_API_TOKEN_RISK_ADMIN``) — grants ONLY
    that role, for separation of duty when the deployment wants it.

Posture:
  * no auth configured at all -> 503 (fail closed; a misconfigured deployment
    cannot mutate state);
  * missing/malformed Authorization header -> 401;
  * a token that grants no role -> 403;
  * a valid token lacking the route's role -> 403;
  * comparison is constant-time (``hmac.compare_digest``); the token is never
    logged nor placed in audit payloads.

Read routes require authentication (``require_authenticated``); only narrowly
scoped liveness (``/health``, ``/mode``) is PUBLIC. The console authenticates by
the token the ``/console`` route injects for the loopback operator.
"""
from __future__ import annotations

import hmac
import os
from enum import Enum

from fastapi import Header, HTTPException

_SCHEME = "Bearer "


class Role(str, Enum):
    """API authorisation roles (separation of duty)."""
    OPERATOR = "operator"                # ordinary operational mutations
    RISK_ADMIN = "risk_admin"            # clear a drawdown breaker (Doc 04 §5)
    TRADE_APPROVER = "trade_approver"    # approve/reject/settle capital actions
    SYSTEM_INTERNAL = "system_internal"  # fire the autonomous daily cycle


_ALL_ROLES = frozenset(Role)


def _admin_token() -> str | None:
    return os.environ.get("ATLAS_API_TOKEN") or None


def _role_token(role: Role) -> str | None:
    return os.environ.get(f"ATLAS_API_TOKEN_{role.name}") or None


def auth_configured() -> bool:
    """True when ANY token is configured — else every protected route is 503."""
    return bool(_admin_token()) or any(_role_token(r) for r in Role)


def console_token() -> str | None:
    """The token the loopback console injects for the single-Principal operator.
    The admin token grants every role; a separation-of-duty deployment (per-role
    tokens, no admin) returns None and the operator supplies a token manually."""
    return _admin_token()


def _roles_for(presented: str) -> frozenset[Role]:
    """The roles a presented bearer token grants (constant-time comparison; the
    admin token grants every role)."""
    admin = _admin_token()
    if admin and hmac.compare_digest(presented, admin):
        return _ALL_ROLES
    granted = {r for r in Role
               if (t := _role_token(r)) and hmac.compare_digest(presented, t)}
    return frozenset(granted)


def _authorize(authorization: str | None, required: Role | None) -> None:
    if not auth_configured():
        raise HTTPException(
            status_code=503,
            detail="API authentication is not configured (set ATLAS_API_TOKEN) — "
                   "protected endpoints are disabled (fail closed).")
    if not authorization or not authorization.startswith(_SCHEME):
        raise HTTPException(status_code=401, detail="missing or malformed bearer token")
    roles = _roles_for(authorization[len(_SCHEME):])
    if not roles:
        raise HTTPException(status_code=403, detail="invalid token")
    if required is not None and required not in roles:
        raise HTTPException(
            status_code=403,
            detail=f"token lacks the required role: {required.value}")


def _dependency(required: Role | None):
    def _dep(authorization: str | None = Header(default=None)) -> None:
        _authorize(authorization, required)
    return _dep


# Named dependencies (module-level singletons so the test harness can bypass them
# all, and so `Depends(...)` identity is stable per role).
require_authenticated = _dependency(None)               # AUTHENTICATED_READ
require_operator = _dependency(Role.OPERATOR)
require_risk_admin = _dependency(Role.RISK_ADMIN)
require_trade_approver = _dependency(Role.TRADE_APPROVER)
require_system_internal = _dependency(Role.SYSTEM_INTERNAL)

# Backward-compat: the trading router imported this name for capital actions.
require_api_auth = require_trade_approver

# The set the test harness overrides to a no-op so ordinary endpoint tests run
# unchanged; real enforcement is proven by the dedicated auth tests (which pop it).
AUTH_DEPENDENCIES = (
    require_authenticated, require_operator, require_risk_admin,
    require_trade_approver, require_system_internal,
)

# Complete inventory of every state-mutating route -> the ROLE it must require
# (§4: classify EVERY route explicitly, deny by default). The conformance test
# (`test_route_security_inventory`) asserts (a) every mutating route in the live
# app appears here AND carries an AUTH_DEPENDENCIES dependency, and (b) each entry
# here carries the RIGHT role — so a newly-added, unclassified mutator FAILS the
# suite rather than shipping open. Path templates are exactly as FastAPI records
# them (router prefix + route path).
#
#   RISK_ADMIN      — clear a drawdown breaker (Doc 04 §5 dual-confirmation)
#   TRADE_APPROVER  — approve/reject/cancel/close/settle capital actions
#   SYSTEM_INTERNAL — fire the autonomous daily cycle
#   OPERATOR        — ordinary operational mutations (research/factory/preflight)
SENSITIVE_ROUTE_ROLES: dict[tuple[str, str], Role] = {
    # Risk (Doc 04 §5)
    ("POST", "/v1/risk/breaker-clearances"): Role.RISK_ADMIN,
    ("POST", "/v1/risk/breaker-clearances/{clearance_id}/confirm"): Role.RISK_ADMIN,
    ("POST", "/v1/risk/preflight"): Role.OPERATOR,
    # System
    ("POST", "/v1/market/instruments/{symbol}/deactivate-delisted"): Role.OPERATOR,
    ("POST", "/v1/system/run-daily"): Role.SYSTEM_INTERNAL,
    # Trading — capital actions
    ("POST", "/v1/trading/proposals/{proposal_id}/approve"): Role.TRADE_APPROVER,
    ("POST", "/v1/trading/proposals/{proposal_id}/reject"): Role.TRADE_APPROVER,
    ("POST", "/v1/trading/orders/{order_id}/cancel"): Role.TRADE_APPROVER,
    ("POST", "/v1/trading/positions/{position_id}/close"): Role.TRADE_APPROVER,
    ("POST", "/v1/trading/settle"): Role.TRADE_APPROVER,
    # Research
    ("POST", "/v1/research/analyze"): Role.OPERATOR,
    ("POST", "/v1/research/opportunities/run"): Role.OPERATOR,
    ("POST", "/v1/research/opportunities/track"): Role.OPERATOR,
    ("POST", "/v1/research/source-picks/ingest"): Role.OPERATOR,
    ("POST", "/v1/research/source-picks/grade"): Role.OPERATOR,
    ("POST", "/v1/research/memos/{memo_id}/review"): Role.OPERATOR,
    # Factory
    ("POST", "/v1/factory/recipes/run"): Role.OPERATOR,
}
