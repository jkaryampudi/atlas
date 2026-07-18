"""Urgent alerting (ops-reliability build, 2026-07): atlas/ops/alerts.py —
once-only latching, unset-URL still-recorded, the expiring-proposal sweep,
and the billing-outage detector's truth table.

Run against a dedicated throwaway DB, never dev 'atlas' or shared 'atlas_test':
    export ATLAS_TEST_DATABASE_URL="postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas_test_ops"

Nothing commits: pg_session rolls back.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

import atlas.ops.alerts as alerts
from atlas.core.clock import FrozenClock
from atlas.ops.alerts import (
    URGENT_EVENT,
    alert_urgent,
    check_expiring_proposals,
    is_billing_outage_error,
    maybe_billing_outage_alert,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

NOW = datetime(2026, 7, 15, 22, 30, tzinfo=UTC)


@pytest.fixture
def s(clean_audit, monkeypatch):
    monkeypatch.delenv("ATLAS_ALERT_URL", raising=False)
    clean_audit.execute(text("SET TIME ZONE 'UTC'"))
    clean_audit.execute(text("UPDATE trading.trade_proposals "
                             "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks",
              "trading.trade_proposals"):
        clean_audit.execute(text(f"DELETE FROM {t}"))
    clean_audit.execute(text(
        "DELETE FROM market.instruments WHERE symbol = 'ZALR'"))
    return clean_audit


def _events(s):
    return s.execute(text(
        "SELECT entity_id, payload FROM audit.decision_events "
        "WHERE event_type = :et ORDER BY seq"), {"et": URGENT_EVENT}).all()


def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.HTTPStatusError(f"HTTP {code}", request=req,
                                 response=httpx.Response(code, request=req))


# ------------------------------------------------------------- alert_urgent

def test_alert_urgent_fires_once_per_kind_key_and_records(s, monkeypatch):
    calls: list[tuple[str, str, str]] = []

    def fake_notify(title, message, *, priority="default"):
        calls.append((title, message, priority))
        return True

    monkeypatch.setattr(alerts, "notify", fake_notify)
    clock = FrozenClock(NOW)
    assert alert_urgent(s, clock, kind="k", key="1", title="T",
                        message="M") is True
    assert alert_urgent(s, clock, kind="k", key="1", title="T",
                        message="M") is False          # latched — no re-page
    assert alert_urgent(s, clock, kind="k", key="2", title="T2",
                        message="M2") is True          # different key fires
    assert [c[0] for c in calls] == ["T", "T2"]        # notify once per key
    evs = _events(s)
    assert [e.entity_id for e in evs] == ["k:1", "k:2"]
    assert evs[0].payload["delivered"] is True
    assert evs[0].payload["priority"] == "high"


def test_unset_alert_url_is_noop_transport_but_still_recorded(s, capsys):
    """The task's hard rule: with ATLAS_ALERT_URL unset the push is a no-op
    (stderr only, delivered=False) but the condition still lands on the audit
    chain — where the morning brief reads it."""
    clock = FrozenClock(NOW)
    assert alert_urgent(s, clock, kind="quiet", key="x", title="T",
                        message="M") is True
    evs = _events(s)
    assert len(evs) == 1
    assert evs[0].payload["delivered"] is False        # honest: not delivered
    assert evs[0].payload["title"] == "T"
    assert "ATLAS_ALERT_URL unset" in capsys.readouterr().err


# ------------------------------------------- expiring-proposal sweep (< 6h)

def _proposal(s, iid, state: str, expires_at: datetime) -> str:
    rc = s.execute(text(
        "INSERT INTO risk.risk_checks (results, verdict, check_kind) "
        "VALUES ('[]', 'PASS', 'proposal') RETURNING id")).scalar()
    return str(s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " origin, signal_ids, entry_price, target_price, position_size, "
        " position_value_aud, state, risk_check_id, expires_at, created_at) "
        "VALUES (:iid, 'US', 'buy', 'core_allocation', '{}', 10, 10, 5, "
        "        50.00, :st, :rc, :exp, :ca) RETURNING id"),
        {"iid": iid, "st": state, "rc": rc, "exp": expires_at,
         "ca": NOW - timedelta(hours=1)}).scalar())


def test_expiring_sweep_pages_once_per_proposal_inside_the_window(s):
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        " instrument_type, name, sector_gics, currency) "
        "VALUES ('ZALR', 'XTEST', 'US', 'etf', 'ZALR', 'Broad', 'USD') "
        "RETURNING id")).scalar()
    clock = FrozenClock(NOW)
    soon = _proposal(s, iid, "pending_approval", NOW + timedelta(hours=3))
    _proposal(s, iid, "pending_approval", NOW + timedelta(hours=20))  # far
    _proposal(s, iid, "pending_approval", NOW - timedelta(minutes=1))  # dead:
    #                                       t2's expire_stale owns the funeral
    _proposal(s, iid, "expired", NOW + timedelta(hours=1))  # not pending

    assert check_expiring_proposals(s, clock) == (soon,)
    evs = _events(s)
    assert len(evs) == 1
    assert evs[0].entity_id == f"proposal_expiring:{soon}"
    assert "3.0h" in evs[0].payload["title"]
    assert "ZALR" in evs[0].payload["title"]

    # ONCE per proposal: any later sweep (hourly cron, next cycle) is silent
    assert check_expiring_proposals(s, clock) == ()
    clock.advance_to(NOW + timedelta(hours=1))
    assert check_expiring_proposals(s, clock) == ()
    assert len(_events(s)) == 1


# ------------------------------------------------- billing-outage detector

def test_is_billing_outage_error_classification():
    """Mirrors runner.py: 4xx-not-429 is the non-transient client class that
    propagates raw; 429/5xx/timeouts are transient; non-HTTP is neither."""
    assert is_billing_outage_error(_http_error(400)) is True
    assert is_billing_outage_error(_http_error(403)) is True
    assert is_billing_outage_error(_http_error(429)) is False   # transient
    assert is_billing_outage_error(_http_error(500)) is False   # transient
    assert is_billing_outage_error(RuntimeError("melted")) is False
    # the 400 buried under wrapping still classifies (cause-chain walk)
    wrapped = RuntimeError("desk wrapper")
    wrapped.__cause__ = _http_error(400)
    assert is_billing_outage_error(wrapped) is True


def _run_row(s, status: str = "ok") -> None:
    s.execute(text(
        "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
        " model, status, cost_usd, created_at) "
        "VALUES ('bull', 'h', 'm', :st, 0.01, :t)"), {"st": status, "t": NOW})


def test_billing_detector_truth_table(s):
    clock = FrozenClock(NOW)
    # transient shapes never fire, rows or no rows
    assert maybe_billing_outage_alert(s, clock, exc=_http_error(429)) is False
    assert maybe_billing_outage_alert(s, clock, exc=_http_error(503)) is False
    assert maybe_billing_outage_alert(s, clock,
                                      exc=RuntimeError("boom")) is False
    assert _events(s) == []

    # ALL calls failed non-transient (zero completed runs today) -> ONE page
    assert maybe_billing_outage_alert(s, clock, exc=_http_error(400)) is True
    evs = _events(s)
    assert len(evs) == 1
    assert evs[0].entity_id == "billing_outage:2026-07-15"
    assert evs[0].payload["priority"] == "high"
    assert "credits exhausted" in evs[0].payload["title"]

    # once per DAY: a second desk death tonight stays silent
    assert maybe_billing_outage_alert(s, clock, exc=_http_error(400)) is False
    assert len(_events(s)) == 1


def test_billing_detector_partial_day_is_not_an_outage(s):
    """A completed call today — ANY status: even a schema_fail or budget_kill
    row proves the vendor answered — vetoes the all-calls-failed signature."""
    clock = FrozenClock(NOW)
    _run_row(s, "schema_fail")
    assert maybe_billing_outage_alert(s, clock, exc=_http_error(400)) is False
    assert _events(s) == []
