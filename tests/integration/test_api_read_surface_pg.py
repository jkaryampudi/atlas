"""Console read-API contracts (Doc 06): every endpoint the dashboard uses,
served against the isolated test DB via TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg


@pytest.fixture
def client(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = clean_audit
    # seed a dedicated instrument + bar; REMOVED at teardown — a lingering
    # active instrument with no bars would (correctly) RED every US gate in
    # the rest of the suite (per-instrument coverage; still fail-closed for
    # bar-less instruments under rules v1.2)
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
    reset_app_engine()


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


def test_risk_limit_set_reports_effective_date_honestly(client):
    """Roundtable catch: limit set v1 activates 2026-07-13 — before that the
    console must say NOT ACTIVE, not pretend the engine is governed."""
    from pathlib import Path

    from atlas.dcp.risk.seed_limits import seed_limit_set
    c, s = client
    seed_limit_set(s, Path(__file__).parents[2] / "seeds" / "limit_set_v1.json")
    s.commit()
    d = c.get("/v1/risk/limit-set/current").json()
    assert d["seeded"] is True and d["version"] == 1 and d["mode"] == "small_aum"
    assert d["effective_from"] == "2026-07-13"
    rules = {r["rule"]: r["value"] for r in d["register"]}
    assert rules["L6"] == 0.01 and rules["L9"] == 2


def test_risk_breakers_ladder(client):
    c, _ = client
    d = c.get("/v1/risk/breakers").json()
    assert d["current_level"] == "NONE" and "NAV" in d["provenance"]
    assert [x["level"] for x in d["ladder"]] == ["DD1", "DD2", "DD3"]


def test_quant_gate_report_surfaces_the_real_fail(client):
    c, _ = client
    d = c.get("/v1/quant/gate-report").json()
    assert d["available"] is True
    avgo = next(x for x in d["symbols"] if x["symbol"] == "AVGO")
    assert avgo["verdict"] == "FAIL"
    assert avgo["null_p"] == 0.059 and avgo["dsr"] == 0.257
    assert "ADR-0004" in d["warning"]


def test_chain_break_is_structured_state_not_500(client):
    from datetime import UTC, datetime

    from atlas.core.audit_repo import PostgresAuditLog
    from atlas.core.clock import FrozenClock
    c, s = client
    log = PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 11, 6, tzinfo=UTC)))
    for i in range(2):
        log.append(event_type="t.e", entity_type="t", entity_id=str(i),
                   actor_type="scheduler", actor_id="t", payload={"i": i})
    s.commit()
    assert c.get("/v1/audit/events/verify").json()["chain"] == "ok"
    s.execute(text("UPDATE audit.decision_events SET payload = CAST(:p AS jsonb) "
                   "WHERE entity_id='0'"), {"p": '{"i": 9}'})
    s.commit()
    d = c.get("/v1/audit/events/verify").json()
    assert d["chain"] == "broken"
    assert d["break_at_seq"] is not None and "mismatch" in d["reason"]


def test_memo_review_write_path(client):
    c, s = client
    memo_id = s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        " conviction, thesis, dissent) "
        "VALUES ('committee','TAPI','REJECT','LOW','t','d') RETURNING id")).scalar()
    s.commit()
    r = c.post(f"/v1/research/memos/{memo_id}/review",
               json={"verdict": "agree", "notes": "solid reasoning"})
    assert r.status_code == 200 and r.json()["reviewed"] >= 1
    # upsert: changing the verdict replaces, not duplicates
    c.post(f"/v1/research/memos/{memo_id}/review", json={"verdict": "disagree"})
    p = c.get("/v1/research/review-progress").json()
    assert p["reviewed"] == 1 and p["disagree"] == 1 and p["target"] == 10
    memo = next(m for m in c.get("/v1/research/memos?symbol=TAPI").json()
                if m["id"] == str(memo_id))
    assert memo["review_verdict"] == "disagree"
    # audited as a HUMAN action
    n = s.execute(text("SELECT count(*) FROM audit.decision_events "
                       "WHERE event_type='memo.review.recorded' "
                       "AND actor_type='human'")).scalar()
    assert n == 2
    assert c.post("/v1/research/memos/00000000-0000-0000-0000-000000000000/review",
                  json={"verdict": "agree"}).status_code == 404


def test_pipeline_runs_jobs_board(client):
    c, s = client
    s.execute(text("DELETE FROM workflow.workflow_node_results WHERE run_id='daily-tapi'"))
    s.execute(text("DELETE FROM workflow.workflow_runs WHERE run_id='daily-tapi'"))
    s.execute(text(
        "INSERT INTO workflow.workflow_runs (run_id, status, started_at, completed_at) "
        "VALUES ('daily-tapi', 'completed', '2026-07-10T23:30Z', '2026-07-10T23:31Z')"))
    s.execute(text(
        "INSERT INTO workflow.workflow_node_results "
        "(run_id, node_name, status, output_ref, completed_at) VALUES "
        "('daily-tapi', 't0_ingest', 'done', 'bars=9 failed=False', '2026-07-10T23:30:20Z'), "
        "('daily-tapi', 't8_report', 'done', 'NAV A$100000', '2026-07-10T23:31:00Z')"))
    s.commit()
    try:
        runs = c.get("/v1/system/pipeline-runs").json()
        run = next(r for r in runs if r["run_id"] == "daily-tapi")
        assert run["status"] == "completed"
        assert [n["node"] for n in run["nodes"]] == ["t0_ingest", "t8_report"]
        assert run["nodes"][0]["result"] == "bars=9 failed=False"
    finally:
        s.execute(text("DELETE FROM workflow.workflow_node_results WHERE run_id='daily-tapi'"))
        s.execute(text("DELETE FROM workflow.workflow_runs WHERE run_id='daily-tapi'"))
        s.commit()
