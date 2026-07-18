"""API contracts for SOURCE PICKS — the console's no-command-line monthly
ingest (Doc 06 envelope discipline):

- POST /v1/research/source-picks/ingest: {started:true} on start, {started:false}
  when busy (honest 200), 400 envelopes for a bad source / bad symbol / no
  tickers / too many; tickers upcased + de-duped before the ops seam; date
  defaults to today, a malformed date is a 400;
- GET /ingest/status: the ops status dict shape;
- GET /source-picks + /source-picks/edge: read-only display shapes.

start_ingest_job is stubbed at the ops seam: the API contract is routing and
validation, never vendors or models. grade/edge read the real table.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import atlas.ops.ingest_picks as picks_mod
from atlas.api.main import app
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg


@pytest.fixture
def client(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    clean_audit.execute(text("TRUNCATE research.source_picks"))
    clean_audit.commit()
    yield TestClient(app), clean_audit
    reset_app_engine()


@pytest.fixture
def capture_start(monkeypatch):
    """Stub the ops seam; first call starts, later calls are busy."""
    calls: list[tuple[str, date, list[str], bool]] = []

    def fake_start(source, recommendation_date, tickers, run_desk=False) -> bool:
        calls.append((source, recommendation_date, tickers, run_desk))
        return len(calls) == 1

    monkeypatch.setattr(picks_mod, "start_ingest_job", fake_start)
    return calls


def test_ingest_starts_upcases_dedupes_then_busy(client, capture_start):
    c, _ = client
    r = c.post("/v1/research/source-picks/ingest",
               json={"source": "investing.com", "date": "2026-07-18",
                     "tickers": ["aapl", "MSFT", "aapl", " nvda "]})
    assert r.status_code == 200 and r.json()["started"] is True
    src, rd, tickers, run_desk = capture_start[0]
    assert src == "investing.com" and rd == date(2026, 7, 18)
    assert tickers == ["AAPL", "MSFT", "NVDA"]              # upcased + de-duped
    assert run_desk is False
    # a second call is busy — honest 200, not an error
    r2 = c.post("/v1/research/source-picks/ingest",
                json={"source": "investing.com", "tickers": ["TSLA"]})
    assert r2.status_code == 200 and r2.json()["started"] is False
    assert "already running" in r2.json()["note"]


def test_ingest_defaults_date_to_today(client, capture_start):
    c, _ = client
    r = c.post("/v1/research/source-picks/ingest",
               json={"source": "investing.com", "tickers": ["AAPL"]})
    assert r.status_code == 200
    assert capture_start[0][1] == date.today() or isinstance(capture_start[0][1], date)


@pytest.mark.parametrize("body,code", [
    ({"source": "", "tickers": ["AAPL"]}, "INVALID_SOURCE"),
    ({"source": "x" * 41, "tickers": ["AAPL"]}, "INVALID_SOURCE"),
    ({"source": "investing.com", "tickers": []}, "NO_TICKERS"),
    ({"source": "investing.com", "tickers": ["BAD$"]}, "INVALID_SYMBOL"),
    ({"source": "investing.com", "tickers": ["AAPL"], "date": "18-07-2026"}, "INVALID_DATE"),
    ({"source": "investing.com", "tickers": [f"T{i}" for i in range(101)]}, "TOO_MANY"),
])
def test_ingest_validation_envelopes(client, capture_start, body, code):
    c, _ = client
    r = c.post("/v1/research/source-picks/ingest", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == code
    assert r.json()["error"]["details"] is None
    assert capture_start == []                              # nothing reached ops


def test_status_endpoint_shape(client):
    c, _ = client
    d = c.get("/v1/research/source-picks/ingest/status").json()
    for key in ("running", "phase", "source", "date", "n_tickers",
                "started_at", "finished_at", "detail", "result"):
        assert key in d
    assert isinstance(d["running"], bool)


def test_list_and_edge_read_recorded_picks(client):
    c, s = client
    # a recorded pick with a graded 20-session outcome (outperformed) and an
    # immature 60. features carry the headline fields the list surfaces.
    s.execute(text(
        "INSERT INTO research.source_picks (source, ticker, recommendation_date, "
        " as_of_session, feature_version, features, excess_20) "
        "VALUES ('investing.com','ZED','2026-01-02','2026-01-02','v1', "
        " CAST(:f AS jsonb), 0.03)"),
        {"f": '{"sector_gics":"Energy","mom_12_1":0.5,"spy_regime":"bull"}'})
    s.commit()
    rows = c.get("/v1/research/source-picks").json()
    z = next(r for r in rows if r["ticker"] == "ZED")
    assert z["source"] == "investing.com" and z["sector"] == "Energy"
    assert z["excess_20"] == 0.03 and z["excess_60"] is None
    edge = {(e["source"], e["horizon"]): e for e in c.get("/v1/research/source-picks/edge").json()}
    e20 = edge[("investing.com", 20)]
    assert e20["n_matured"] == 1 and e20["outperform_rate"] == 1.0
