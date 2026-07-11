from fastapi.testclient import TestClient

from atlas.api.main import app


def test_health_reports_paper_mode_and_never_armed():
    r = TestClient(app).get("/v1/system/health")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_mode"] == "paper"
    assert body["armed"] is False
    assert body["limit_mode"] == "small_aum"
