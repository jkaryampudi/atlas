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


def test_research_shadow_xsmom_sleeve_labelled_non_authoritative(lab):
    _seed(lab, "research_shadow")
    body = TestClient(app).get("/v1/portfolio/attribution/daily").json()
    xs = [r for r in body["rows"] if r["sleeve"] == "xsmom"]
    assert xs and all(r["authoritative"] is False for r in xs)
    assert all(r["validation_status"] == "research_shadow" for r in xs)
    # structural sleeves carry no verdict field
    tot = [r for r in body["rows"] if r["sleeve"] == "total"]
    assert tot and "authoritative" not in tot[0]
    # the composite alpha is flagged non-authoritative
    assert body["satellite_alpha_authoritative"] is False
    assert body["satellite_alpha_validation_status"] == "research_shadow"
    # values are unchanged (still the seeded value, only labelled)
    assert xs[0]["value_aud"] == "8000.00"


def test_paper_xsmom_sleeve_labelled_validated(lab):
    _seed(lab, "paper")
    body = TestClient(app).get("/v1/portfolio/attribution/daily").json()
    xs = [r for r in body["rows"] if r["sleeve"] == "xsmom"]
    assert xs and all(r["authoritative"] is True for r in xs)
    assert all(r["validation_status"] == "validated" for r in xs)
    assert body["satellite_alpha_authoritative"] is True
