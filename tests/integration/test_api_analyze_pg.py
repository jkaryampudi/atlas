"""API contracts for ANALYZE-ANY-TICKER (Doc 06 envelope discipline):

- POST /v1/research/analyze: {started: true} on start, {started: false} when
  busy (honest, still 200 — same contract as /v1/system/run-daily), 400
  envelope for a bad symbol or an over-long source, symbol upcased before it
  reaches the ops layer, blank source stored as NULL;
- GET /v1/research/analyze/status: the ops status dict shape;
- GET /v1/research/memos: `source` is an additive field — present when
  tagged, null otherwise (backward compatible).

start_analysis is stubbed at the ops seam: the API contract is about routing
and validation, never about vendors or models.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import atlas.ops.analyze as analyze_mod
from atlas.api.main import app
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg


@pytest.fixture
def client(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    yield TestClient(app), clean_audit
    reset_app_engine()


@pytest.fixture
def capture_start(monkeypatch):
    """Stub the ops seam; first call starts, later calls are busy."""
    calls: list[tuple[str, str | None]] = []

    def fake_start(symbol: str, source: str | None) -> bool:
        calls.append((symbol, source))
        return len(calls) == 1

    monkeypatch.setattr(analyze_mod, "start_analysis", fake_start)
    return calls


def test_analyze_starts_then_answers_busy_honestly(client, capture_start):
    c, _ = client
    r = c.post("/v1/research/analyze",
               json={"symbol": "nvda", "source": "investing.com"})
    assert r.status_code == 200
    assert r.json()["started"] is True
    assert capture_start[0] == ("NVDA", "investing.com")   # upcased, tag verbatim

    r2 = c.post("/v1/research/analyze", json={"symbol": "NVDA"})
    assert r2.status_code == 200                            # busy is not an error
    assert r2.json()["started"] is False
    assert "already running" in r2.json()["note"]


def test_blank_source_becomes_null(client, capture_start):
    c, _ = client
    r = c.post("/v1/research/analyze", json={"symbol": "brk.b", "source": ""})
    assert r.status_code == 200
    assert capture_start[0] == ("BRK.B", None)


@pytest.mark.parametrize("bad", ["", "TOO_LONG_SYM", "ELEVENCHARS", "BAD$CHAR",
                                 "spaced out", "ünï"])
def test_analyze_rejects_bad_symbols_with_the_envelope(client, capture_start, bad):
    c, _ = client
    r = c.post("/v1/research/analyze", json={"symbol": bad})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "INVALID_SYMBOL"
    assert body["error"]["details"] is None
    assert capture_start == []                             # nothing reached ops


def test_analyze_rejects_overlong_source(client, capture_start):
    c, _ = client
    r = c.post("/v1/research/analyze",
               json={"symbol": "NVDA", "source": "x" * 41})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_SOURCE"
    assert capture_start == []
    # exactly 40 chars is fine, stored verbatim
    r = c.post("/v1/research/analyze",
               json={"symbol": "NVDA", "source": "y" * 40})
    assert r.status_code == 200
    assert capture_start[0] == ("NVDA", "y" * 40)


def test_status_endpoint_shape(client):
    c, _ = client
    r = c.get("/v1/research/analyze/status")
    assert r.status_code == 200
    d = r.json()
    for key in ("running", "phase", "symbol", "source", "started_at",
                "finished_at", "detail", "result"):
        assert key in d
    assert isinstance(d["running"], bool)


def test_memos_carries_the_source_tag_additively(client):
    c, s = client
    s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        "conviction, thesis, evidence_refs, source) "
        "VALUES ('committee', 'ZAPI', 'WATCHLIST', 'LOW', 'tagged memo', '[]', "
        "'investing.com')"))
    s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        "conviction, thesis, evidence_refs) "
        "VALUES ('committee', 'ZAPI', 'WATCHLIST', 'LOW', 'untagged memo', '[]')"))
    s.commit()
    rows = c.get("/v1/research/memos?symbol=ZAPI").json()
    by_thesis = {r["thesis"]: r for r in rows}
    assert by_thesis["tagged memo"]["source"] == "investing.com"
    assert by_thesis["untagged memo"]["source"] is None    # backward compatible
