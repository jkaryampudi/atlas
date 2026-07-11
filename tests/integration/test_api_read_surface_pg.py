"""Console read-API contracts (Doc 06): every endpoint the dashboard uses,
served against the isolated test DB via TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import atlas.core.db as db
from atlas.api.main import app
from tests.conftest import URL, requires_pg

pytestmark = requires_pg


@pytest.fixture
def client(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    monkeypatch.setattr(db, "_session_factory", None)
    s = clean_audit
    # seed a dedicated instrument + bar; REMOVED at teardown — a lingering
    # active instrument with no bars would (correctly) RED every US gate in
    # the rest of the suite (per-instrument coverage rules v1.1)
    s.execute(text("INSERT INTO market.instruments (symbol, exchange, market, "
                   "instrument_type, name, currency) VALUES "
                   "('TAPI','NYSE','US','stock','Test Api Corp','USD') "
                   "ON CONFLICT (symbol, exchange) DO NOTHING"))
    s.execute(text("INSERT INTO market.price_bars_daily "
                   "(instrument_id, bar_date, open, high, low, close, volume, source) "
                   "SELECT id, '2026-07-10', 10, 11, 9, 10.5, 100, 'EodhdAdapter' "
                   "FROM market.instruments WHERE symbol='TAPI' "
                   "ON CONFLICT (instrument_id, bar_date) DO NOTHING"))
    s.commit()
    yield TestClient(app), s
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol='TAPI')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol='TAPI'"))
    s.commit()
    monkeypatch.setattr(db, "_session_factory", None)


def test_freshness_reports_markets_and_gates(client):
    c, s = client
    r = c.get("/v1/market/freshness")
    assert r.status_code == 200
    us = next(m for m in r.json() if m["market"] == "US")
    assert us["bars"] >= 1 and us["latest_bar"] >= "2026-07-10"


def test_bars_endpoint_returns_series(client):
    c, _ = client
    r = c.get("/v1/market/bars/TAPI?days=5")
    assert r.status_code == 200
    body = r.json()
    assert body and float(body[-1]["close"]) == 10.5


def test_fx_endpoint(client):
    c, s = client
    s.execute(text("INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
                   "VALUES ('USD','AUD','2026-07-10',1.44,'EodhdAdapter') "
                   "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate=1.44"))
    s.commit()
    r = c.get("/v1/market/fx?days=5")
    assert r.status_code == 200
    assert any(row["rate_date"] == "2026-07-10" for row in r.json())


def test_quant_trials_and_verdicts(client):
    c, s = client
    s.execute(text("INSERT INTO quant.trial_registry (strategy_family, spec_hash, metrics) "
                   "VALUES ('apitest','h1', CAST('{\"sharpe\": 0.5}' AS jsonb))"))
    s.commit()
    r = c.get("/v1/quant/trials?family=apitest")
    assert r.status_code == 200
    assert r.json()[0]["metrics"]["sharpe"] == 0.5
    assert c.get("/v1/quant/verdicts").status_code == 200
    s.execute(text("DELETE FROM quant.trial_registry WHERE strategy_family='apitest'"))
    s.commit()


def test_research_memos_and_cost(client):
    c, _ = client
    r = c.get("/v1/research/memos")
    assert r.status_code == 200  # empty list on a clean DB is fine
    cost = c.get("/v1/research/cost").json()
    assert cost["daily_cap_usd"] > 0
    assert cost["remaining_usd"] <= cost["daily_cap_usd"]
