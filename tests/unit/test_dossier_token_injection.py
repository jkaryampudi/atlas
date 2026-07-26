"""F-016: the /console/dossier route injects the loopback operator token at serve
time, exactly like /console, so the dossier's ANALYZE-WITH-DESK mutator can
authenticate. Before this the dossier was served as a raw file with no token, so
its POST /v1/research/analyze always failed auth (503 when no token configured,
401 when one was) — and the frontend mislabelled that as "desk busy". The token
is never written to the static file; unconfigured => the placeholder stays empty
(mutators then fail closed 503), so no bogus token is baked in."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import atlas.api.main as main_mod
from atlas.api.main import app

TOKEN = "dossier-loopback-token-xyz"


def test_dossier_injects_token_when_configured(monkeypatch):
    monkeypatch.setenv("ATLAS_API_TOKEN", TOKEN)
    html = TestClient(app).get("/console/dossier").text
    assert f'window.__ATLAS_TOKEN__ = "{TOKEN}";' in html


def test_dossier_leaves_placeholder_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ATLAS_API_TOKEN", raising=False)
    from atlas.api.auth import Role
    for r in Role:
        monkeypatch.delenv(f"ATLAS_API_TOKEN_{r.name}", raising=False)
    html = TestClient(app).get("/console/dossier").text
    assert 'window.__ATLAS_TOKEN__ = "";' in html
    assert TOKEN not in html


def test_dossier_never_ships_token_in_static_file():
    """The token must be injected, never persisted — the on-disk file carries only
    the empty placeholder, and the analyze POST sends the Authorization header."""
    disk = Path(main_mod._DOSSIER).read_text(encoding="utf-8")
    assert 'window.__ATLAS_TOKEN__ = "";' in disk
    # the mutator fetch must carry the operator token (the fix's whole point)
    assert "...AUTH" in disk
    # a 503/401 must no longer be mislabelled as a busy desk
    assert "desk auth not configured" in disk
