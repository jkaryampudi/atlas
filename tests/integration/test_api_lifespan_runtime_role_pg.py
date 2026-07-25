"""F-019/F-020 CI hardening: the API LIFESPAN enforces the least-privilege runtime.

These tests boot the real FastAPI lifespan (via TestClient) under the deployed
secure-by-default posture (`ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true`) and assert:

  * connected as the least-privilege ``atlas_app`` role -> the app STARTS and
    ``/health`` reports least_privilege + ENABLE-ALWAYS audit triggers;
  * connected as the superuser owner -> the app REFUSES to start (fail closed).

The existing test_db_least_privilege_pg proves the DB-layer bypass refusals; this
proves the same posture is wired into the actual application startup path, so a CI
run — not just a unit helper — exercises the runtime-role gate.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from tests.conftest import URL, _ensure_test_db, requires_pg, reset_app_engine

pytestmark = requires_pg


@pytest.fixture(autouse=True)
def _bootstrap_test_db():
    """Migrate atlas_test (creating the atlas_app role + ENABLE-ALWAYS audit
    triggers) before each lifespan test, exactly as the sibling
    test_db_least_privilege_pg does via its app_engine fixture. Without this the
    file free-rides on an earlier-collected test having set conftest._prepared, so
    it fails when run in isolation or under pytest-xdist (each worker starts with
    its own _prepared=False and no atlas_app role). _ensure_test_db is idempotent."""
    _ensure_test_db()

# NB: URL.__str__ masks the password to '***'; render with hide_password=False so
# the value we put in ATLAS_DATABASE_URL carries the real credential.
APP_URL = make_url(URL).set(
    username="atlas_app", password="atlas_app_local_only",
).render_as_string(hide_password=False)
OWNER_URL = make_url(URL).render_as_string(hide_password=False)   # test DB owner (superuser)


def _boot(monkeypatch, db_url: str, *, require_lp: str):
    """Point the app at db_url with the given least-privilege flag and (re)build
    its engine, so the lifespan runs against that role."""
    monkeypatch.setenv("ATLAS_DATABASE_URL", db_url)
    monkeypatch.setenv("ATLAS_DB_REQUIRE_LEAST_PRIVILEGE", require_lp)
    monkeypatch.setenv("ATLAS_INPROC_SCHEDULER", "0")
    reset_app_engine()
    from atlas.api.main import app
    return app


def test_lifespan_starts_as_atlas_app_and_health_is_least_privilege(monkeypatch):
    app = _boot(monkeypatch, APP_URL, require_lp="true")
    try:
        with TestClient(app) as c:                      # runs the startup lifespan
            body = c.get("/v1/system/health").json()
        assert body["status"] == "ok"
        assert body["db_require_least_privilege"] is True
        dp = body["db_privilege"]
        assert dp["role"] == "atlas_app"
        assert dp["is_superuser"] is False
        assert dp["least_privilege"] is True
        assert dp["can_bypass_triggers"] is False
        assert dp["audit_triggers_enable_always"] is True
    finally:
        reset_app_engine()


def test_lifespan_refuses_superuser_runtime_when_least_privilege_required(monkeypatch):
    app = _boot(monkeypatch, OWNER_URL, require_lp="true")
    try:
        with pytest.raises(RuntimeError, match="SUPERUSER|bypass the audit"):
            with TestClient(app):                        # lifespan must raise on startup
                pass
    finally:
        reset_app_engine()


def test_lifespan_audit_wall_assertion_runs_unconditionally(monkeypatch):
    """Even with the runtime-role check opted OUT, the app still asserts the audit
    wall is ENABLE ALWAYS (0044) — so a DB missing that hardening cannot boot."""
    app = _boot(monkeypatch, APP_URL, require_lp="false")
    try:
        with TestClient(app) as c:
            body = c.get("/v1/system/health").json()
        assert body["db_privilege"]["audit_triggers_enable_always"] is True
    finally:
        reset_app_engine()
