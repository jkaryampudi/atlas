from fastapi.testclient import TestClient

from atlas.api.main import app


def test_health_reports_paper_mode_and_never_armed():
    r = TestClient(app).get("/v1/system/health")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_mode"] == "paper"
    assert body["armed"] is False
    assert body["limit_mode"] == "small_aum"


def test_health_reports_db_privilege_posture():
    """F-019/F-020: /health surfaces the DB runtime role's privilege posture, and
    never 500s on the probe. In dev/test the connection is the owner (superuser),
    but db_require_least_privilege is off, so status stays ok while the posture is
    reported honestly."""
    r = TestClient(app).get("/v1/system/health")
    assert r.status_code == 200
    body = r.json()
    assert "db_privilege" in body
    assert body["db_require_least_privilege"] is False
    assert body["status"] == "ok"
    dp = body["db_privilege"]
    # the probe ran against the test DB and reported a role
    if dp.get("checked"):
        assert "is_superuser" in dp and "least_privilege" in dp
