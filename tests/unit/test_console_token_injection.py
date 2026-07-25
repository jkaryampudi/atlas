"""F-016: the /console route injects the loopback operator token at serve time so
the sole control surface can authenticate mutators, WITHOUT the token ever living
in the static file. Unconfigured => the placeholder stays empty (mutators then
fail closed 503), so no bogus token is baked in."""
from __future__ import annotations

from fastapi.testclient import TestClient

from atlas.api.main import app

TOKEN = "console-loopback-token-abc"


def test_console_injects_token_when_configured(monkeypatch):
    monkeypatch.setenv("ATLAS_API_TOKEN", TOKEN)
    html = TestClient(app).get("/console").text
    assert f'window.__ATLAS_TOKEN__ = "{TOKEN}";' in html


def test_console_leaves_placeholder_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ATLAS_API_TOKEN", raising=False)
    from atlas.api.auth import Role
    for r in Role:
        monkeypatch.delenv(f"ATLAS_API_TOKEN_{r.name}", raising=False)
    html = TestClient(app).get("/console").text
    assert 'window.__ATLAS_TOKEN__ = "";' in html
    assert TOKEN not in html


def test_console_never_ships_token_in_static_file():
    """The token must be injected, never persisted — the on-disk file carries only
    the empty placeholder."""
    from pathlib import Path

    import atlas.api.main as main_mod
    disk = Path(main_mod._CONSOLE).read_text(encoding="utf-8")
    assert 'window.__ATLAS_TOKEN__ = "";' in disk
