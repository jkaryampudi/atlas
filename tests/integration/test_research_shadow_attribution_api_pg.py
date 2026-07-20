"""P0.1 (ADR-0018) — GET /v1/portfolio/attribution/daily labels each satellite
sleeve from its backing strategy's state: a research_shadow xsmom sleeve is
returned with authoritative=false / validation_status='research_shadow' (never
as validated), a paper sleeve as validated, and structural sleeves (core/total)
carry no verdict. Values/returns are never changed by the label."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

D1, D2 = date(2026, 7, 13), date(2026, 7, 14)
CA = datetime(2026, 7, 13, tzinfo=UTC)


def _seed(s, state: str) -> None:
    s.execute(text("DELETE FROM reporting.attribution_daily"))
    s.execute(text("DELETE FROM quant.strategies "
                   "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))
    s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state) VALUES ('xsmom-pit-tr','xsmom_pit','1.0.0',"
        " '{}','sha','{}',:st)"), {"st": state})
    for d in (D1, D2):
        for sleeve, val in (("xsmom", "8000"), ("total", "100000")):
            s.execute(text(
                "INSERT INTO reporting.attribution_daily (session_date, sleeve, "
                " value_aud, ret_1d, benchmark_ret_1d, created_at) "
                "VALUES (:d,:sl,:v,0.01,0.01,:ca)"),
                {"d": d, "sl": sleeve, "v": val, "ca": CA})
    s.commit()


@pytest.fixture
def lab(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    yield clean_audit
    clean_audit.execute(text("DELETE FROM reporting.attribution_daily"))
    clean_audit.execute(text("DELETE FROM quant.strategies "
                             "WHERE family IN ('xsmom-pit-tr', 'pead-sue-tr')"))
    clean_audit.commit()
    reset_app_engine()


def test_default_scope_excludes_the_shadow_sleeve(lab):
    """ADR-0018: the DEFAULT (authoritative_portfolio) view omits a research_shadow
    sleeve entirely — its performance is never in the default response — and the
    scope envelope marks the response authoritative with no shadow results."""
    _seed(lab, "research_shadow")
    body = TestClient(app).get("/v1/portfolio/attribution/daily").json()
    assert body["performance_scope"] == "authoritative_portfolio"
    assert body["authoritative"] is True
    assert body["contains_shadow_results"] is False
    # the shadow xsmom sleeve is NOT shown in the authoritative view
    assert [r for r in body["rows"] if r["sleeve"] == "xsmom"] == []
    # structural sleeves remain
    assert [r for r in body["rows"] if r["sleeve"] == "total"]


def test_research_shadow_scope_shows_only_the_labelled_shadow_sleeve(lab):
    """Explicit scope=research_shadow returns the shadow-only view, labelled."""
    _seed(lab, "research_shadow")
    body = TestClient(app).get(
        "/v1/portfolio/attribution/daily?scope=research_shadow").json()
    assert body["performance_scope"] == "research_shadow"
    assert body["authoritative"] is False
    assert body["caveat"] == "RESEARCH SHADOW — NOT VALIDATED"
    xs = [r for r in body["rows"] if r["sleeve"] == "xsmom"]
    assert xs and all(r["authoritative"] is False for r in xs)
    assert all(r["validation_status"] == "research_shadow" for r in xs)
    assert xs[0]["value_aud"] == "8000.00"          # value unchanged, only scoped
    # the shadow view shows ONLY shadow satellites — no structural sleeves
    assert [r for r in body["rows"] if r["sleeve"] == "total"] == []


def test_all_simulated_scope_is_explicit_and_non_authoritative(lab):
    _seed(lab, "research_shadow")
    body = TestClient(app).get(
        "/v1/portfolio/attribution/daily?scope=all_simulated").json()
    assert body["performance_scope"] == "all_simulated"
    assert body["authoritative"] is False
    assert body["contains_shadow_results"] is True
    assert body["caveat"] == "COMBINED SIMULATION — NON-AUTHORITATIVE"


def test_unknown_scope_is_refused(lab):
    r = TestClient(app).get("/v1/portfolio/attribution/daily?scope=include_all")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_SCOPE"


def test_paper_xsmom_sleeve_labelled_validated(lab):
    _seed(lab, "paper")
    body = TestClient(app).get("/v1/portfolio/attribution/daily").json()
    assert body["performance_scope"] == "authoritative_portfolio"
    xs = [r for r in body["rows"] if r["sleeve"] == "xsmom"]
    assert xs and all(r["authoritative"] is True for r in xs)
    assert all(r["validation_status"] == "validated" for r in xs)
    assert body["authoritative"] is True
