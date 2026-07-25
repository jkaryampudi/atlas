"""F-016: central, deny-by-default authentication + role authorisation on every
state-mutating API route.

These tests deliberately POP the conftest auth override so the production
dependencies run for real, and assert the fail-closed posture end to end:

  * no token configured anywhere            -> 503 (mutations disabled)
  * missing / malformed Authorization       -> 401
  * a token that grants no role             -> 403
  * a valid token lacking the route's role  -> 403 (separation of duty)
  * the admin token                          -> clears every role
  * a per-role token                         -> clears ONLY its role
  * read/liveness routes                     -> stay open (PUBLIC)

The final tests are the route-inventory conformance guard: every mutating route
in the live app must be classified in ``SENSITIVE_ROUTE_ROLES`` AND carry the
correct role dependency — a newly-added, unclassified mutator fails the suite
rather than shipping open.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from atlas.api import auth
from atlas.api.auth import Role
from atlas.api.main import app

ADMIN = "admin-token-all-roles-xyz789"
OP = "operator-token-111"
RISK = "risk-admin-token-222"
TRADE = "trade-approver-token-333"
SYS = "system-internal-token-444"

_APPROVE = "/v1/trading/proposals/00000000-0000-0000-0000-000000000000/approve"
_SETTLE = "/v1/trading/settle"
_BREAKER = "/v1/risk/breaker-clearances"
_RUN_DAILY = "/v1/system/run-daily"

ROLE_DEP = {
    Role.OPERATOR: auth.require_operator,
    Role.RISK_ADMIN: auth.require_risk_admin,
    Role.TRADE_APPROVER: auth.require_trade_approver,
    Role.SYSTEM_INTERNAL: auth.require_system_internal,
}


@pytest.fixture
def real_auth():
    """Remove EVERY conftest allow-override so real auth+authz runs here."""
    for dep in auth.AUTH_DEPENDENCIES:
        app.dependency_overrides.pop(dep, None)
    yield
    # conftest's autouse fixture re-installs the overrides for the next test


def _clear_all_tokens(monkeypatch):
    monkeypatch.delenv("ATLAS_API_TOKEN", raising=False)
    for r in Role:
        monkeypatch.delenv(f"ATLAS_API_TOKEN_{r.name}", raising=False)


def _set_admin(monkeypatch):
    _clear_all_tokens(monkeypatch)
    monkeypatch.setenv("ATLAS_API_TOKEN", ADMIN)


def _set_per_role(monkeypatch):
    _clear_all_tokens(monkeypatch)
    monkeypatch.setenv("ATLAS_API_TOKEN_OPERATOR", OP)
    monkeypatch.setenv("ATLAS_API_TOKEN_RISK_ADMIN", RISK)
    monkeypatch.setenv("ATLAS_API_TOKEN_TRADE_APPROVER", TRADE)
    monkeypatch.setenv("ATLAS_API_TOKEN_SYSTEM_INTERNAL", SYS)


def _bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------- fail closed

def test_unconfigured_disables_mutations_fail_closed(real_auth, monkeypatch):
    _clear_all_tokens(monkeypatch)
    r = TestClient(app).post(_APPROVE, json={"acknowledged_risks": True})
    assert r.status_code == 503


def test_run_daily_never_anonymous(real_auth, monkeypatch):
    """The autonomous cycle trigger (system.py) must never fire unauthenticated:
    503 when unconfigured, 401 with no header."""
    _clear_all_tokens(monkeypatch)
    assert TestClient(app).post(_RUN_DAILY).status_code == 503
    _set_admin(monkeypatch)
    assert TestClient(app).post(_RUN_DAILY).status_code == 401


def test_missing_header_is_401(real_auth, monkeypatch):
    _set_admin(monkeypatch)
    r = TestClient(app).post(_APPROVE, json={"acknowledged_risks": True})
    assert r.status_code == 401


def test_malformed_header_is_401(real_auth, monkeypatch):
    _set_admin(monkeypatch)
    r = TestClient(app).post(_SETTLE, headers={"Authorization": ADMIN})  # no 'Bearer '
    assert r.status_code == 401


def test_unknown_token_is_403(real_auth, monkeypatch):
    _set_admin(monkeypatch)
    r = TestClient(app).post(_SETTLE, headers=_bearer("nope-not-a-real-token"))
    assert r.status_code == 403


def test_wrong_length_token_is_403_not_error(real_auth, monkeypatch):
    """A token of a different length must be rejected cleanly (constant-time
    compare handles unequal lengths without raising)."""
    _set_admin(monkeypatch)
    r = TestClient(app).post(_SETTLE, headers=_bearer("x"))
    assert r.status_code == 403


# ------------------------------------------------------------- admin grants all

def test_admin_token_clears_every_role(real_auth, monkeypatch):
    _set_admin(monkeypatch)
    c = TestClient(app)
    # NB: run-daily / settle are proven via 401/403 gating elsewhere — invoking
    # them with a live token has real side effects, so the admin-passes check uses
    # routes whose handlers no-op cleanly on an empty test DB (approve->404,
    # breaker->409 nothing-to-clear).
    for path, body in [
        (_APPROVE, {"acknowledged_risks": True}),
        (_BREAKER, {"reason": "test"}),
    ]:
        r = c.post(path, headers=_bearer(ADMIN), json=body)
        assert r.status_code not in (401, 403, 503), (path, r.status_code)


# --------------------------------------------------------- separation of duty

def test_operator_token_cannot_approve_trades(real_auth, monkeypatch):
    """OPERATOR is not TRADE_APPROVER — a trade action is 403 for the operator."""
    _set_per_role(monkeypatch)
    r = TestClient(app).post(_APPROVE, headers=_bearer(OP),
                             json={"acknowledged_risks": True})
    assert r.status_code == 403


def test_operator_token_cannot_clear_breaker(real_auth, monkeypatch):
    """Breaker clearance requires RISK_ADMIN specifically (Doc 04 §5)."""
    _set_per_role(monkeypatch)
    r = TestClient(app).post(_BREAKER, headers=_bearer(OP), json={"reason": "x"})
    assert r.status_code == 403


def test_risk_admin_token_clears_breaker_role(real_auth, monkeypatch):
    _set_per_role(monkeypatch)
    r = TestClient(app).post(_BREAKER, headers=_bearer(RISK), json={"reason": "x"})
    assert r.status_code not in (401, 403, 503)


def test_trade_approver_token_clears_trade_role_only(real_auth, monkeypatch):
    """TRADE_APPROVER clears a capital action but cannot fire the system cycle."""
    _set_per_role(monkeypatch)
    c = TestClient(app)
    ok = c.post(_APPROVE, headers=_bearer(TRADE), json={"acknowledged_risks": True})
    assert ok.status_code not in (401, 403, 503)
    assert c.post(_RUN_DAILY, headers=_bearer(TRADE)).status_code == 403


# ---------------------------------------------------------------- reads open

def test_read_and_liveness_routes_stay_open(real_auth, monkeypatch):
    _set_admin(monkeypatch)
    c = TestClient(app)
    for path in ("/v1/system/health", "/v1/system/mode"):
        assert c.get(path).status_code not in (401, 403, 503)


# ----------------------------------------------------- route-inventory guard

def _route_dep_calls(route: APIRoute) -> set:
    """Every dependency callable attached to a route (route.dependencies + the
    signature deps), flattened."""
    seen: set = set()
    stack = list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        if d.call is not None:
            seen.add(d.call)
        stack.extend(d.dependencies)
    return seen


def _iter_api_routes(application):
    """Yield (full_path, APIRoute) for EVERY route in the app, transparently
    descending the custom `_IncludedRouter` mounts this app uses for
    `include_router` (routes are nested under `original_router` with the prefix
    on `include_context`)."""
    for route in application.routes:
        if isinstance(route, APIRoute):
            yield route.path, route
        elif hasattr(route, "original_router") and hasattr(route, "include_context"):
            prefix = getattr(route.include_context, "prefix", "") or ""
            for sub in route.original_router.routes:
                if isinstance(sub, APIRoute):
                    yield prefix + sub.path, sub


def test_route_security_inventory():
    """CONFORMANCE: every state-mutating route in the live app is (a) classified
    in SENSITIVE_ROUTE_ROLES, (b) carries an auth dependency, and (c) carries the
    RIGHT role dependency. An unclassified/unprotected mutator FAILS here."""
    auth_deps = set(auth.AUTH_DEPENDENCIES)
    live_keys: set[tuple[str, str]] = set()
    for full_path, route in _iter_api_routes(app):
        mutating = route.methods - {"GET", "HEAD", "OPTIONS"}
        if not mutating:
            continue
        calls = _route_dep_calls(route)
        for method in mutating:
            key = (method, full_path)
            live_keys.add(key)
            assert key in auth.SENSITIVE_ROUTE_ROLES, (
                f"unclassified mutating route {key} — add it to "
                "SENSITIVE_ROUTE_ROLES and gate it (deny by default)")
            assert calls & auth_deps, f"mutating route {key} carries no auth dependency"
            required = auth.SENSITIVE_ROUTE_ROLES[key]
            assert ROLE_DEP[required] in calls, (
                f"route {key} must require role {required.value}")
    # Sanity: the walker actually found the app's mutators (guards against a
    # future routing change that hides them and makes this test vacuous).
    assert len(live_keys) >= 15, f"route walker found only {len(live_keys)} mutators"
    # No stale classifications: every declared entry maps to a real route.
    stale = set(auth.SENSITIVE_ROUTE_ROLES) - live_keys
    assert not stale, f"SENSITIVE_ROUTE_ROLES names non-existent routes: {stale}"


def test_auth_uses_constant_time_compare():
    """The token comparison must be hmac.compare_digest (constant time) — guard
    against a regression to ==."""
    import inspect

    src = inspect.getsource(auth._roles_for)
    assert "compare_digest" in src
