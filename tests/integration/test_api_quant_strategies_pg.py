"""GET /v1/quant/strategies — the console STRATEGY card's read surface
(ADR-0010): strategy row + active signal count + next rebalance + the latest
band reading, read-only. Seeds are committed for the TestClient and removed
at teardown (the api-test convention)."""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from atlas.dcp.signals.xsmom.generate import is_month_end_session
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

BANDS = {"max_drawdown_from_sleeve_peak": -0.40,
         "trailing_126_session_excess_vs_spy_tr_pp": -25.0}


@pytest.fixture
def client(monkeypatch, pg_session):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = pg_session

    def _wipe() -> None:
        s.execute(text("DELETE FROM quant.sleeve_daily"))
        s.execute(text("DELETE FROM quant.signals"))
        s.execute(text(
            "DELETE FROM quant.strategies WHERE family = 'xsmom-pit-tr'"))
        s.execute(text("DELETE FROM market.instruments WHERE symbol = 'ZQAP'"))

    _wipe()
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state, approved_by, approved_at) "
        "VALUES ('xsmom-pit-tr', 'xsmom_pit', '1.0.0', '{}', 'sha', "
        "        CAST(:b AS jsonb), 'paper', 'Principal (test)', "
        "        '2026-07-13T00:00:00+00:00') RETURNING id"),
        {"b": json.dumps(BANDS)}).scalar()
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES ('ZQAP', 'XTEST', 'US', 'stock', 'ZQAP', 'USD') "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid, :iid, '2026-07-13', 'long', 1, 0.5, '2027-12-31', "
        "        '2026-07-13T00:00:00+00:00')"), {"sid": sid, "iid": iid})
    s.execute(text(
        "INSERT INTO quant.sleeve_daily (strategy_id, session_date, "
        " sleeve_value, spy_tr_close, peak_value, drawdown, excess_126s_pp, "
        " created_at) "
        "VALUES (:sid, '2026-07-13', 9000, 130, 10000, -0.1, NULL, "
        "        '2026-07-13T00:00:00+00:00')"), {"sid": sid})
    s.commit()
    yield TestClient(app)
    _wipe()
    s.commit()
    reset_app_engine()


def test_research_shadow_labelled_separately_from_validated(monkeypatch, pg_session):
    """Objective 7e (ADR-0018): a downgraded research_shadow strategy is still
    listed on the STRATEGY card (not silently hidden) but labelled
    authoritative=false / validation_status='research_shadow', while a paper
    strategy reads authoritative=true / 'validated'. Shadow performance can
    never be mistaken for validated performance."""
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = pg_session
    fams = ("zz-paper-test", "zz-shadow-test")
    s.execute(text("DELETE FROM quant.strategies WHERE family = ANY(:f)"),
              {"f": list(fams)})
    s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state, approved_by, approved_at) "
        "VALUES ('zz-paper-test','p','1.0.0','{}','sha','{}','paper',"
        " 'Principal (test)', '2026-07-13T00:00:00+00:00')"))
    s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state, shadowed_at) "
        "VALUES ('zz-shadow-test','sh','1.0.0','{}','sha','{}',"
        " 'research_shadow', '2026-07-20T00:00:00+00:00')"))
    s.commit()
    try:
        rows = {x["family"]: x for x in
                TestClient(app).get("/v1/quant/strategies").json()}
        assert rows["zz-paper-test"]["authoritative"] is True
        assert rows["zz-paper-test"]["validation_status"] == "validated"
        # shown (not hidden) AND clearly non-validated
        assert "zz-shadow-test" in rows
        assert rows["zz-shadow-test"]["authoritative"] is False
        assert rows["zz-shadow-test"]["validation_status"] == "research_shadow"
    finally:
        s.execute(text("DELETE FROM quant.strategies WHERE family = ANY(:f)"),
                  {"f": list(fams)})
        s.commit()
        reset_app_engine()


def test_strategies_endpoint_renders_the_card_facts(client):
    r = client.get("/v1/quant/strategies")
    assert r.status_code == 200
    rows = [x for x in r.json() if x["family"] == "xsmom-pit-tr"]
    assert len(rows) == 1
    x = rows[0]
    assert (x["name"], x["version"], x["state"]) == ("xsmom_pit", "1.0.0", "paper")
    assert x["approved_by"] == "Principal (test)"
    assert x["active_signals"] == 1
    assert x["tolerance_bands"]["max_drawdown_from_sleeve_peak"] == -0.40
    # structural: the endpoint quotes the generator's own calendar — a real
    # month-end US session in the near future (no wall-clock golden: the
    # suite must pass on any run date)
    nxt = date.fromisoformat(x["next_rebalance"])
    assert is_month_end_session(nxt)
    assert date.today() < nxt <= date.today().replace(
        year=date.today().year + 1)
    b = x["band_status"]
    assert b["session_date"] == "2026-07-13"
    assert b["sleeve_value"] == 9000.0 and b["drawdown"] == -0.1
    assert b["excess_126s_pp"] is None      # dormant renders as null
