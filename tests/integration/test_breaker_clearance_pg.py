"""DD2/DD3 breaker clearance (Doc 04 §5): "Resumption from DD2/DD3 requires
the dual-confirmation human action; breaker state changes are audit events
and cannot be cleared by agents."

Exercises the full resumption flow against atlas_test: a latched DD2 (NAV
100k -> 87k -> 95k), confirmation A (request, audited), a too-soon
confirmation B (DUAL_CONFIRM_TOO_SOON — refused in code AND unrepresentable
by hand thanks to the table CHECK), the real confirmation after the 1h gap
(latched level steps down to the pinned recovery target DD1), and the proof
that build_proposal actually sees the cleared breaker. Refusal paths:
nothing to clear, second pending request, unknown id, double confirm, blank
reason. Plus the HTTP contract (Doc 06 §3.3 envelope) in the
test_api_trading_pg style.

Nothing dcp-level is committed (pg_session rolls back); the API fixture
commits and wipes after itself, exactly like test_api_trading_pg.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from atlas.api.main import app
from atlas.api.routers import risk as risk_router
from atlas.core.clock import FrozenClock
from atlas.dcp.risk.clearance import (
    confirm_clearance,
    latched_breaker_level,
    request_clearance,
)
from atlas.dcp.risk.engine import BreakerLevel
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.proposals import build_proposal
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
# Monday 2026-07-13: the first day limit set v1 is effective (seeds/limit_set_v1.json)
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)


# ------------------------------------------------------------------- seeding

def _clean(s) -> None:
    """Remove any committed debris from crashed runs (FK-safe order)."""
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots",
              "risk.breaker_clearances"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol = 'ZTLC')"))
    s.execute(text("DELETE FROM research.memos WHERE instrument_symbol = 'ZTLC'"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol = 'ZTLC'"))


def _snapshots(s, navs: list[int]) -> None:
    """Daily NAV marks ending 2026-07-12 — 100k -> 87k -> 95k latches DD2
    (-13% trough) while the stateless view of -5% says DD1."""
    first = date(2026, 7, 13) - timedelta(days=len(navs))
    for i, nav in enumerate(navs):
        s.execute(text(
            "INSERT INTO trading.portfolio_snapshots "
            "(as_of, nav_aud, cash_aud, open_risk_pct) "
            "VALUES (:at, :nav, :nav, 0)"),
            {"at": datetime(first.year, first.month, first.day, 21, 0, tzinfo=UTC)
                   + timedelta(days=i),
             "nav": nav})


def _events(s) -> list[str]:
    return [r[0] for r in s.execute(text(
        "SELECT event_type FROM audit.decision_events ORDER BY seq")).all()]


# ------------------------------------------------------------- the dcp flow

def test_dual_confirmation_clears_the_latch_with_pinned_recovery_level(clean_audit):
    s = clean_audit
    _clean(s)
    _snapshots(s, [100_000, 87_000, 95_000])
    clock = FrozenClock(T0)
    assert latched_breaker_level(s) is BreakerLevel.DD2

    # --- confirmation A: pending row + audit event
    cid = request_clearance(s, clock, reason="post-mortem filed; book re-underwritten")
    row = s.execute(text(
        "SELECT from_level, reason, requested_by, requested_at, confirmed_at "
        "FROM risk.breaker_clearances WHERE id = :c"), {"c": cid}).one()
    assert (row.from_level, row.requested_by) == ("DD2", "principal")
    assert row.requested_at == T0
    assert row.confirmed_at is None
    assert _events(s).count("drawdown.breaker.clear_requested") == 1
    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'drawdown.breaker.clear_requested'")).one()
    assert ev.payload["from_level"] == "DD2"
    assert ev.payload["reason"] == "post-mortem filed; book re-underwritten"
    # a pending request does NOT move the fold — only confirmation B does
    assert latched_breaker_level(s) is BreakerLevel.DD2

    # --- confirmation B too soon: refused with the Doc 06 §3.3 code
    clock.advance_to(T0 + timedelta(minutes=30))
    with pytest.raises(ValueError, match="DUAL_CONFIRM_TOO_SOON"):
        confirm_clearance(s, clock, clearance_id=cid)
    assert s.execute(text(
        "SELECT confirmed_at FROM risk.breaker_clearances WHERE id = :c"),
        {"c": cid}).scalar() is None

    # --- confirmation B after the gap: latch steps down to the COMPUTED
    # target of the last known drawdown (95k vs 100k HWM = -5% -> DD1), pinned
    clock.advance_to(T0 + timedelta(hours=1, minutes=1))
    assert confirm_clearance(s, clock, clearance_id=cid) is BreakerLevel.DD1
    assert latched_breaker_level(s) is BreakerLevel.DD1
    assert s.execute(text(
        "SELECT confirmed_at FROM risk.breaker_clearances WHERE id = :c"),
        {"c": cid}).scalar() == T0 + timedelta(hours=1, minutes=1)
    cleared = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'drawdown.breaker.cleared'")).one()
    assert cleared.payload["from_level"] == "DD2"
    assert cleared.payload["to"] == "DD1"
    assert cleared.payload["reason"] == "post-mortem filed; book re-underwritten"

    # --- a clearance clears once; DD1 has nothing left to clear
    with pytest.raises(ValueError, match="already confirmed"):
        confirm_clearance(s, clock, clearance_id=cid)
    with pytest.raises(ValueError, match="nothing to clear"):
        request_clearance(s, clock, reason="again")


def test_check_constraint_rejects_manual_too_soon_confirmation(clean_audit):
    """Even a hand-written UPDATE cannot confirm early — the ≥1h gap is a
    table CHECK (risk.limit_sets precedent), not just application code."""
    s = clean_audit
    _clean(s)
    _snapshots(s, [100_000, 87_000, 95_000])
    cid = request_clearance(s, FrozenClock(T0), reason="tamper probe")
    with pytest.raises(IntegrityError):
        with s.begin_nested():
            s.execute(text(
                "UPDATE risk.breaker_clearances "
                "SET confirmed_at = requested_at + interval '30 minutes' "
                "WHERE id = :c"), {"c": cid})


def test_refusal_paths(clean_audit):
    s = clean_audit
    _clean(s)
    clock = FrozenClock(T0)

    # nothing to clear on an empty NAV history (breaker NONE)...
    with pytest.raises(ValueError, match="nothing to clear"):
        request_clearance(s, clock, reason="no drawdown exists")
    # ...or under a plain DD1 (it tracks the drawdown; no latch to clear)
    _snapshots(s, [100_000, 94_000])
    assert latched_breaker_level(s) is BreakerLevel.DD1
    with pytest.raises(ValueError, match="nothing to clear"):
        request_clearance(s, clock, reason="DD1 is not clearable")

    # latch DD2, then: blank reason refused, second pending refused
    s.execute(text("DELETE FROM trading.portfolio_snapshots"))
    _snapshots(s, [100_000, 87_000, 95_000])
    with pytest.raises(ValueError, match="reason"):
        request_clearance(s, clock, reason="   ")
    request_clearance(s, clock, reason="first request")
    with pytest.raises(ValueError, match="already pending"):
        request_clearance(s, clock, reason="second request")

    # unknown id
    with pytest.raises(ValueError, match="unknown clearance"):
        confirm_clearance(s, clock, clearance_id=str(uuid4()))


def test_build_proposal_sees_the_cleared_breaker(clean_audit):
    """DD2 rejects a new position (rule DD); after the dual-confirmed
    clearance the same proposal passes — and the live fold at build time sees
    NONE, because the live NAV (the A$100k paper seed, flat book) sits at the
    high-water mark once the latched memory is cleared."""
    s = clean_audit
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    _snapshots(s, [100_000, 87_000, 95_000])
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) "
        "VALUES ('ZTLC', 'XTEST', 'US', 'stock', 'ZTLC', 'Information Technology', "
        "'USD') RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, close, volume, source) "
        "VALUES (:iid, :d, 100, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)} for i in range(21)])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2026-07-10', 1.5, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = 1.5"))
    memo_id = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        "evidence_refs) VALUES ('committee', 'ZTLC', 'BUY', '[]') "
        "RETURNING id")).scalar())
    clock = FrozenClock(T0)

    def _build():
        return build_proposal(
            s, clock, memo_id=memo_id, symbol="ZTLC", signal_refs=[str(uuid4())],
            entry_price=Decimal("100"), stop_price=Decimal("95"),
            target_price=Decimal("120"))

    # latched DD2: no new positions (Doc 04 §5) — terminal FAIL on rule DD
    rejected = _build()
    assert rejected.state == "rejected"
    assert "DD" in rejected.failures

    cid = request_clearance(s, clock, reason="review complete; resume")
    clock.advance_to(T0 + timedelta(hours=1, minutes=1))
    confirm_clearance(s, clock, clearance_id=cid)

    # cleared: the SAME proposal now passes, and the check records the breaker
    passed = _build()
    assert passed.state == "pending_approval"
    assert passed.verdict == "PASS"
    assert passed.qty == 53          # L1 binds: 8% of A$100k / A$150
    breaker = s.execute(text(
        "SELECT price_snapshot ->> 'breaker' FROM risk.risk_checks "
        "WHERE id = :c"), {"c": passed.risk_check_id}).scalar()
    assert breaker == "none"


# ------------------------------------------------------- the HTTP contract

@pytest.fixture
def capi(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    clock = FrozenClock(T0)
    monkeypatch.setattr(risk_router, "_clock", lambda: clock)
    s = clean_audit
    _clean(s)
    _snapshots(s, [100_000, 87_000, 95_000])
    s.commit()
    yield TestClient(app), s, clock
    _clean(s)
    s.commit()
    reset_app_engine()


def test_api_clearance_contract(capi):
    c, s, clock = capi
    b = c.get("/v1/risk/breakers").json()
    assert b["current_level"] == "DD2"
    assert "latched fold" in b["provenance"]

    # confirmation A
    r = c.post("/v1/risk/breaker-clearances", json={"reason": "resume after review"})
    assert r.status_code == 200
    cid = r.json()["clearance_id"]
    assert r.json()["status"] == "pending_confirmation"
    listed = c.get("/v1/risk/breaker-clearances").json()
    assert listed[0]["id"] == cid and listed[0]["pending"] is True
    assert listed[0]["confirmable_after"] == (T0 + timedelta(hours=1)).isoformat()

    # a second request while one is pending: 409 INVALID_STATE
    dup = c.post("/v1/risk/breaker-clearances", json={"reason": "again"})
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "INVALID_STATE"

    # confirmation B too soon: 409 DUAL_CONFIRM_TOO_SOON (Doc 06 §3.3)
    clock.advance_to(T0 + timedelta(minutes=45))
    soon = c.post(f"/v1/risk/breaker-clearances/{cid}/confirm")
    assert soon.status_code == 409
    err = soon.json()["error"]
    assert err["code"] == "DUAL_CONFIRM_TOO_SOON"
    assert "DUAL_CONFIRM_TOO_SOON" in err["message"]
    # the refusal committed nothing
    assert s.execute(text(
        "SELECT confirmed_at FROM risk.breaker_clearances WHERE id = :c"),
        {"c": cid}).scalar() is None

    # unknown id: 404 in the same envelope
    missing = c.post(f"/v1/risk/breaker-clearances/{uuid4()}/confirm")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"

    # confirmation B after the gap: cleared, and /breakers reports the step-down
    clock.advance_to(T0 + timedelta(hours=1, minutes=5))
    done = c.post(f"/v1/risk/breaker-clearances/{cid}/confirm")
    assert done.status_code == 200
    assert done.json() == {"status": "cleared", "clearance_id": cid,
                           "latched_level": "DD1"}
    assert c.get("/v1/risk/breakers").json()["current_level"] == "DD1"
    assert c.get("/v1/risk/breaker-clearances").json()[0]["pending"] is False

    # DD1 has nothing to clear: 409 INVALID_STATE
    calm = c.post("/v1/risk/breaker-clearances", json={"reason": "no latch left"})
    assert calm.status_code == 409
    assert calm.json()["error"]["code"] == "INVALID_STATE"
