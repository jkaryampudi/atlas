"""Trading API contracts (Doc 06 §3): the approval desk over HTTP.

Seeds a full lifecycle state via the dcp functions (FrozenClock at the limit
set's first effective day), COMMITS it, then exercises the endpoints exactly
as the console will — including both 409 outcomes (RISK_RECHECK_FAILED with
itemised failures, PROPOSAL_EXPIRED) and the §3.3 error envelope shape.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from atlas.api.routers import trading as trading_router
from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.proposals import build_proposal
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)   # limit set v1 first effective day
NEXT_SESSION = date(2026, 7, 14)


def _wipe(s) -> None:
    # leave pending_approval too: the §2.1 CHECK forbids it without a check ref
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE source = 'tapi-test'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol = 'ZTAPI')"))
    s.execute(text("DELETE FROM research.memos WHERE instrument_symbol = 'ZTAPI'"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol = 'ZTAPI'"))


@pytest.fixture
def tclient(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    clock = FrozenClock(T0)
    monkeypatch.setattr(trading_router, "_clock", lambda: clock)

    s = clean_audit
    _wipe(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) VALUES "
        "('ZTAPI', 'XTEST', 'US', 'stock', 'ZTAPI', 'Information Technology', 'USD') "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        "volume, source) VALUES (:iid, :d, 100, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)} for i in range(21)])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2026-07-10', 1.5, 'tapi-test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = 1.5"))
    memo_id = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        "thesis, kill_criteria, evidence_refs) "
        "VALUES ('committee', 'ZTAPI', 'BUY', 'test thesis', "
        "        '[\"thesis broken\"]', '[]') RETURNING id")).scalar())
    res = build_proposal(
        s, clock, memo_id=memo_id, symbol="ZTAPI", signal_refs=[str(uuid4())],
        entry_price=Decimal("100"), stop_price=Decimal("95"),
        target_price=Decimal("120"))
    assert res.state == "pending_approval"
    s.commit()

    yield TestClient(app), s, clock, res
    _wipe(s)
    s.commit()
    reset_app_engine()


def test_list_and_detail_render_evidence_bundle(tclient):
    c, _, _, res = tclient
    listed = c.get("/v1/trading/proposals?state=pending_approval").json()
    assert [p["id"] for p in listed] == [res.proposal_id]
    assert listed[0]["symbol"] == "ZTAPI"
    assert listed[0]["check_verdict"] == "PASS"

    d = c.get(f"/v1/trading/proposals/{res.proposal_id}").json()
    assert d["state"] == "pending_approval"
    assert d["investment_thesis"] == "test thesis"
    assert d["kill_criteria"] == ["thesis broken"]
    assert d["signal_ids"] and d["committee_memo_id"]
    # Doc 05 §4 / Doc 06 §3.1: itemised numeric results, not just prose
    results = d["risk_checks"][0]["results"]
    l1 = next(r for r in results if r["rule"] == "L1")
    assert l1["pass"] is True and l1["value"] is not None and l1["limit"] == 0.08
    assert c.get(f"/v1/trading/proposals/{uuid4()}").status_code == 404


def test_approve_requires_acknowledged_risks(tclient):
    c, _, _, res = tclient
    r = c.post(f"/v1/trading/proposals/{res.proposal_id}/approve",
               json={"acknowledged_risks": False})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "RISKS_NOT_ACKNOWLEDGED"


def test_approve_happy_path_then_terminal(tclient):
    c, s, _, res = tclient
    r = c.post(f"/v1/trading/proposals/{res.proposal_id}/approve",
               json={"acknowledged_risks": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved" and body["order_id"]
    assert body["risk_check_id"] != res.risk_check_id     # fresh §2.2 check
    order = c.get("/v1/trading/orders?state=pending_submit").json()
    assert order[0]["id"] == body["order_id"]

    # approve/reject have no second act
    again = c.post(f"/v1/trading/proposals/{res.proposal_id}/approve",
                   json={"acknowledged_risks": True})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "INVALID_STATE"

    # cancel the pending order -> proposal voided; cancelling twice is 409
    cancel = c.post(f"/v1/trading/orders/{body['order_id']}/cancel",
                    json={"reason": "test"})
    assert cancel.status_code == 200
    assert c.post(f"/v1/trading/orders/{body['order_id']}/cancel",
                  json={"reason": "again"}).status_code == 409
    state = s.execute(text(
        "SELECT state FROM trading.trade_proposals WHERE id = :p"),
        {"p": res.proposal_id}).scalar()
    assert state == "voided"


def test_recheck_fail_returns_409_with_itemised_failures(tclient):
    c, s, _, res = tclient
    limits_v2 = dict(s.execute(text(
        "SELECT limits FROM risk.limit_sets WHERE version = 1")).scalar())
    limits_v2["L1_max_stock_weight"] = 0.005
    s.execute(text(
        "INSERT INTO risk.limit_sets (version, mode, limits, effective_from, "
        "created_by, confirmation_a, confirmation_b) "
        "VALUES (2, 'small_aum', CAST(:l AS jsonb), '2026-07-13', 'principal:test', "
        "        :t - interval '2 hours', :t)"),
        {"l": json.dumps(limits_v2), "t": T0})
    s.commit()

    r = c.post(f"/v1/trading/proposals/{res.proposal_id}/approve",
               json={"acknowledged_risks": True})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "RISK_RECHECK_FAILED"
    assert any(f.startswith("L1") for f in err["details"]["failures"])
    # the void COMMITTED despite the 409 — terminal, no orders
    state = s.execute(text(
        "SELECT state FROM trading.trade_proposals WHERE id = :p"),
        {"p": res.proposal_id}).scalar()
    assert state == "voided"
    assert s.execute(text("SELECT count(*) FROM trading.orders")).scalar() == 0


def test_expired_approval_and_reject_return_409(tclient):
    c, s, clock, res = tclient
    clock.advance_to(T0 + timedelta(hours=25))
    r = c.post(f"/v1/trading/proposals/{res.proposal_id}/approve",
               json={"acknowledged_risks": True})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PROPOSAL_EXPIRED"
    state = s.execute(text(
        "SELECT state FROM trading.trade_proposals WHERE id = :p"),
        {"p": res.proposal_id}).scalar()
    assert state == "expired"    # the transition committed with the 409
    rj = c.post(f"/v1/trading/proposals/{res.proposal_id}/reject",
                json={"reason": "late"})
    assert rj.status_code == 409
    assert rj.json()["error"]["code"] == "INVALID_STATE"


def test_settle_endpoint_fills_when_session_data_arrives(tclient):
    c, s, clock, res = tclient
    approved = c.post(f"/v1/trading/proposals/{res.proposal_id}/approve",
                      json={"acknowledged_risks": True}).json()
    assert approved["status"] == "approved"

    # overnight: nothing to fill yet
    assert c.post("/v1/trading/settle").json()["fills"] == []

    iid = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'ZTAPI'")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        "volume, source) VALUES (:iid, :d, 102, 103, 1000000, 'EodhdAdapter')"),
        {"iid": iid, "d": NEXT_SESSION})
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', :d, 1.5, 'tapi-test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = 1.5"),
        {"d": NEXT_SESSION})
    s.commit()
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))

    body = c.post("/v1/trading/settle").json()
    assert len(body["fills"]) == 1
    assert body["fills"][0]["order_id"] == approved["order_id"]
    assert body["fills"][0]["fill_price"] == pytest.approx(102.102)
    filled = c.get("/v1/trading/orders?state=filled").json()
    assert filled[0]["shortfall_bps"] is not None
