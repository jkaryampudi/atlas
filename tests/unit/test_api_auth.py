"""F-016: real authentication enforcement on state-mutating API endpoints.

These tests deliberately POP the test-wide auth override (conftest) so the
production dependency runs for real. They assert the fail-closed posture:
unset token -> 503, missing/malformed header -> 401, wrong token -> 403, correct
token -> the endpoint runs (past auth). A read endpoint stays open.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlas.api.auth import require_api_auth
from atlas.api.main import app

TOKEN = "test-operator-token-abc123"


@pytest.fixture
def real_auth():
    """Remove the conftest allow-override so real auth runs for this test."""
    app.dependency_overrides.pop(require_api_auth, None)
    yield
    # conftest's autouse fixture re-installs the override for the next test


def test_unconfigured_token_disables_mutations_fail_closed(real_auth, monkeypatch):
    monkeypatch.delenv("ATLAS_API_TOKEN", raising=False)
    r = TestClient(app).post("/v1/trading/proposals/00000000-0000-0000-0000-000000000000/approve",
                             json={"acknowledged_risks": True})
    assert r.status_code == 503


def test_missing_header_is_401(real_auth, monkeypatch):
    monkeypatch.setenv("ATLAS_API_TOKEN", TOKEN)
    r = TestClient(app).post("/v1/trading/proposals/00000000-0000-0000-0000-000000000000/approve",
                             json={"acknowledged_risks": True})
    assert r.status_code == 401


def test_malformed_header_is_401(real_auth, monkeypatch):
    monkeypatch.setenv("ATLAS_API_TOKEN", TOKEN)
    r = TestClient(app).post("/v1/trading/settle", headers={"Authorization": TOKEN})  # no 'Bearer '
    assert r.status_code == 401


def test_wrong_token_is_403(real_auth, monkeypatch):
    monkeypatch.setenv("ATLAS_API_TOKEN", TOKEN)
    r = TestClient(app).post("/v1/trading/settle",
                             headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 403


def test_correct_token_passes_auth(real_auth, monkeypatch):
    """A correct token clears auth; the endpoint then runs (and, with no DB set
    up here, fails downstream — NOT at 401/403/503). We only assert auth passed."""
    monkeypatch.setenv("ATLAS_API_TOKEN", TOKEN)
    r = TestClient(app).post("/v1/trading/proposals/00000000-0000-0000-0000-000000000000/approve",
                             headers={"Authorization": f"Bearer {TOKEN}"},
                             json={"acknowledged_risks": True})
    assert r.status_code not in (401, 403, 503)


def test_read_endpoint_stays_open(real_auth, monkeypatch):
    monkeypatch.setenv("ATLAS_API_TOKEN", TOKEN)
    r = TestClient(app).get("/v1/system/health")
    assert r.status_code not in (401, 403, 503)
