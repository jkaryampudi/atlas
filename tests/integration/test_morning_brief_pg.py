"""The morning brief (ops-reliability build, 2026-07): assembled, persisted,
deterministic — reporting.morning_brief (migration 0031) + brief.py.

Run against a dedicated throwaway DB, never dev 'atlas' or shared 'atlas_test':
    export ATLAS_TEST_DATABASE_URL="postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas_test_ops"

The GOLDEN test constructs one session by hand — cycle node rows including a
FAILED desk (the 400-credit line), an approval queue with one proposal 3.0h
from death and one at 20.0h, a committee memo, stored attribution, a
CUSUM-latched strategy, and tonight's urgent-alert events — and pins the
ENTIRE assembled jsonb against a hand-assembled expected document: expiry
countdowns, the FAILED node, and the billing-outage signature included.
Then: idempotent upsert (one row per session, payload replaced in place),
the jsonb round-trip, the API shape (200 + 404), and the desk-spend-$0.00
heuristic's truth table.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.reporting.brief import assemble_brief, latest_brief, persist_brief
from atlas.ops.alerts import check_expiring_proposals, maybe_billing_outage_alert
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

DAY = date(2026, 7, 15)
NOW = datetime(2026, 7, 15, 22, 30, tzinfo=UTC)
T7_LINE = ("desk FAILED: Client error '400 Bad Request' for url "
           "'https://api.anthropic.com/v1/messages' — credit balance too low")
T9_LINE = ("NAV A$100000 · fills 0, stops fired 0 · desk idle · bridge idle · "
           "signals idle · pead signals idle · bands idle · cusum idle · "
           "attribution idle · core idle · scorecard: none matured · "
           "learning: nothing newly matured · ingest clean · desk FAILED — see log")


def _clean(s) -> None:
    s.execute(text("SET TIME ZONE 'UTC'"))   # deterministic isoformat round-trips
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM reporting.morning_brief"))
    s.execute(text("DELETE FROM reporting.attribution_daily"))
    s.execute(text("DELETE FROM quant.sleeve_daily"))
    s.execute(text("DELETE FROM quant.signals"))
    s.execute(text("DELETE FROM quant.strategies WHERE family = 'xsmom-pit-tr'"))
    s.execute(text("DELETE FROM workflow.workflow_node_results "
                   "WHERE run_id LIKE 'daily-%'"))
    s.execute(text("DELETE FROM workflow.workflow_runs WHERE run_id LIKE 'daily-%'"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol = 'ZBRF'"))


def _node(s, run_id: str, name: str, status: str, ref: str, at: datetime) -> None:
    s.execute(text(
        "INSERT INTO workflow.workflow_node_results "
        "(run_id, node_name, status, output_ref, completed_at) "
        "VALUES (:r, :n, :st, :ref, :at)"),
        {"r": run_id, "n": name, "st": status, "ref": ref, "at": at})


def _check(s) -> str:
    return str(s.execute(text(
        "INSERT INTO risk.risk_checks (results, verdict, check_kind) "
        "VALUES ('[]', 'PASS', 'proposal') RETURNING id")).scalar())


def _billing_error() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.HTTPStatusError(
        "Client error '400 Bad Request'", request=req,
        response=httpx.Response(400, request=req))


def _construct_session(s, clock: FrozenClock) -> dict[str, str]:
    """The hand-constructed session the golden pins (module docstring)."""
    run_id = f"daily-{DAY.isoformat()}"
    s.execute(text(
        "INSERT INTO workflow.workflow_runs (run_id, started_at, status, "
        " completed_at) VALUES (:r, :t0, 'completed', :t1)"),
        {"r": run_id, "t0": NOW - timedelta(hours=1), "t1": NOW})
    _node(s, run_id, "t0_ingest", "done", "bars=12 failed=False",
          NOW - timedelta(minutes=30))
    _node(s, run_id, "t7_desk", "done", T7_LINE, NOW - timedelta(minutes=20))
    _node(s, run_id, "t9_report", "done", T9_LINE, NOW - timedelta(minutes=10))

    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        " instrument_type, name, sector_gics, currency) "
        "VALUES ('ZBRF', 'XTEST', 'US', 'etf', 'ZBRF', 'Broad', 'USD') "
        "RETURNING id")).scalar()
    memo_id = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        " recommendation, conviction, evidence_refs, created_at) "
        "VALUES ('committee', 'ZBRF', 'BUY', 'HIGH', '[]', :t) RETURNING id"),
        {"t": NOW - timedelta(minutes=25)}).scalar())
    core_pid = str(s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " origin, signal_ids, entry_price, target_price, position_size, "
        " position_value_aud, state, risk_check_id, expires_at, created_at) "
        "VALUES (:iid, 'US', 'buy', 'core_allocation', '{}', 10, 10, 100, "
        "        1000.00, 'pending_approval', :rc, :exp, :ca) RETURNING id"),
        {"iid": iid, "rc": _check(s), "exp": NOW + timedelta(hours=3),
         "ca": NOW - timedelta(hours=69)}).scalar())
    agent_pid = str(s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        " position_size, position_value_aud, state, risk_check_id, expires_at, "
        " created_at) "
        "VALUES (:iid, 'US', 'buy', :m, :sig, 10, 9, 12, 200, 2000.00, "
        "        'pending_approval', :rc, :exp, :ca) RETURNING id"),
        {"iid": iid, "m": memo_id, "sig": [memo_id],   # any uuid array works
         "rc": _check(s), "exp": NOW + timedelta(hours=20),
         "ca": NOW - timedelta(hours=4)}).scalar())

    s.execute(text(
        "INSERT INTO reporting.attribution_daily (session_date, sleeve, "
        " value_aud, ret_1d, benchmark_ret_1d, created_at) VALUES "
        "(:d, 'core', 14930.00, NULL, NULL, :t), "
        "(:d, 'total', 100000.00, 0.01, 0.02, :t)"), {"d": DAY, "t": NOW})

    sid = str(s.execute(text(
        "INSERT INTO quant.strategies (family, name, version, spec, code_sha, "
        " tolerance_bands, state) "
        "VALUES ('xsmom-pit-tr', 'xsmom_pit', '1.0.0', '{}', 'test-sha', "
        "        CAST(:b AS jsonb), 'paper') RETURNING id"),
        {"b": json.dumps({})}).scalar())
    s.execute(text(
        "INSERT INTO quant.sleeve_daily (strategy_id, session_date, "
        " sleeve_value, peak_value, drawdown, created_at) "
        "VALUES (:sid, :d, 12345.67, 13000.00, -0.1, :t)"),
        {"sid": sid, "d": DAY, "t": NOW})
    PostgresAuditLog(s, clock).append(
        event_type="quant.strategy.cusum_breach", entity_type="strategy",
        entity_id=sid, actor_type="dcp", actor_id="cusum_check",
        payload={"family": "xsmom-pit-tr"})

    # tonight's urgent alerts, through the REAL paths: the billing detector
    # (zero agent_runs today + the raw 400) then the expiring-proposal sweep
    # (the 3.0h core proposal is inside the 6h window; 20.0h is not)
    assert maybe_billing_outage_alert(s, clock, exc=_billing_error()) is True
    assert check_expiring_proposals(s, clock) == (core_pid,)
    return {"core_pid": core_pid, "agent_pid": agent_pid, "memo_id": memo_id}


@pytest.fixture
def constructed(clean_audit, monkeypatch):
    monkeypatch.delenv("ATLAS_ALERT_URL", raising=False)
    monkeypatch.setenv("ATLAS_DAILY_LLM_BUDGET_USD", "10.0")
    monkeypatch.delenv("ATLAS_BUDGET_NIGHTLY", raising=False)
    s = clean_audit
    _clean(s)
    clock = FrozenClock(NOW)
    ids = _construct_session(s, clock)
    return s, clock, ids


def test_brief_golden_payload(constructed):
    """THE golden: the whole assembled document, hand-assembled expected —
    expiry countdowns, the FAILED node, and the billing signature included."""
    s, clock, ids = constructed
    brief = assemble_brief(s, clock)
    assert brief.session_date == DAY

    expected = {
        "session_date": "2026-07-15",
        "generated_at": "2026-07-15T22:30:00+00:00",
        "cycle": {
            "run_id": "daily-2026-07-15",
            "run_status": "completed",
            "nodes": [
                {"node": "t0_ingest", "status": "done",
                 "result": "bars=12 failed=False"},
                {"node": "t7_desk", "status": "done", "result": T7_LINE},
                {"node": "t9_report", "status": "done", "result": T9_LINE},
            ],
            "failed_nodes": ["t7_desk", "t9_report"],
        },
        "queue": {
            "proposals": [
                {"id": ids["core_pid"], "symbol": "ZBRF", "action": "buy",
                 "origin": "core_allocation", "qty": 100,
                 "value_aud": "1000.00",
                 "expires_at": "2026-07-16T01:30:00+00:00",
                 "hours_left": 3.0, "expiring_soon": True},
                {"id": ids["agent_pid"], "symbol": "ZBRF", "action": "buy",
                 "origin": "agent", "qty": 200, "value_aud": "2000.00",
                 "expires_at": "2026-07-16T18:30:00+00:00",
                 "hours_left": 20.0, "expiring_soon": False},
            ],
            "expiring_soon_count": 1,
        },
        "memos": [
            {"id": ids["memo_id"], "memo_type": "committee", "symbol": "ZBRF",
             "recommendation": "BUY", "conviction": "HIGH", "source": None,
             "proposal_state": "pending_approval"},
        ],
        "attribution": {
            "session_date": "2026-07-15",
            "sleeves": {
                "core": {"value_aud": "14930.00", "ret_1d": None,
                         "benchmark_ret_1d": None},
                "total": {"value_aud": "100000.00", "ret_1d": 0.01,
                          "benchmark_ret_1d": 0.02},
            },
            "line": ("attribution 2026-07-15: core A$14930.00 · xsmom n/a · "
                     "pead n/a · cash n/a · total A$100000.00"),
            "performance_scope": "authoritative_portfolio",
            "authoritative": True,
            "satellite_alpha_pp": None,
            "contains_shadow_results": False,
            "caveat": "",
            "shadow_sleeves": [],
        },
        "strategies": [
            {"family": "xsmom-pit-tr", "state": "paper",
             "session": "2026-07-15", "sleeve_value": "12345.67",
             "drawdown": -0.1, "excess_126s_pp": None,
             "cusum_breached": True, "demoted_today": False},
        ],
        "learning_line": "learning: nothing newly matured",
        "budget": {"spend_usd": 0.0, "daily_cap_usd": 10.0,
                   "nightly_watermark_usd": 6.0, "runs_by_status": {}},
        "desk": {"line": T7_LINE, "expected": True, "failed": True,
                 "memo_count": 1, "spend_usd": 0.0},
        "urgent_alerts": [
            {"key": "billing_outage:2026-07-15", "kind": "billing_outage",
             "title": "Atlas: API credits exhausted — desk skipped",
             "priority": "high", "delivered": False,
             "at": "2026-07-15T22:30:00+00:00"},
            {"key": f"proposal_expiring:{ids['core_pid']}",
             "kind": "proposal_expiring",
             "title": "Atlas: proposal expiring in 3.0h — ZBRF still awaits "
                      "your seal",
             "priority": "high", "delivered": False,
             "at": "2026-07-15T22:30:00+00:00"},
        ],
        "flags": {
            "no_cycle_run": False,
            "failed_nodes": ["t7_desk", "t9_report"],
            "expiring_proposals": [ids["core_pid"]],
            "band_or_cusum_events": ["xsmom-pit-tr"],
            "billing_outage_suspected": True,
        },
    }
    assert brief.payload == expected
    assert brief.summary() == (
        "brief 2026-07-15: queue 2 (1 expiring soon) · failed nodes 2 · "
        "memos 1 · spend $0.00 · BILLING-OUTAGE SIGNATURE")


def test_brief_persist_is_idempotent_one_row_per_session(constructed):
    s, clock, _ = constructed
    first = persist_brief(s, clock)
    count = s.execute(text(
        "SELECT count(*) FROM reporting.morning_brief")).scalar()
    assert count == 1
    stored = latest_brief(s)
    assert stored is not None
    assert stored["payload"] == first.payload        # jsonb round-trip is exact
    assert stored["session_date"] == "2026-07-15"

    # same instant -> byte-identical re-assembly, still one row
    again = persist_brief(s, clock)
    assert again.payload == first.payload
    assert s.execute(text(
        "SELECT count(*) FROM reporting.morning_brief")).scalar() == 1

    # a later same-day re-assembly REPLACES in place: countdowns tick down,
    # the row does not duplicate, updated_at moves, created_at does not
    clock.advance_to(NOW + timedelta(hours=1))
    later = persist_brief(s, clock)
    assert later.payload["queue"]["proposals"][0]["hours_left"] == 2.0
    row = s.execute(text(
        "SELECT count(*) AS n, min(created_at) AS c, min(updated_at) AS u "
        "FROM reporting.morning_brief")).one()
    assert row.n == 1
    assert row.c == NOW and row.u == NOW + timedelta(hours=1)
    # one brief-assembled audit event per assembly (three so far)
    n_ev = s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'reporting.brief.assembled'")).scalar()
    assert n_ev == 3


def test_api_shape_latest_and_404(constructed, monkeypatch):
    """GET /v1/reporting/brief/latest serves the persisted row verbatim; an
    empty table is an honest 404, never an empty fabrication."""
    s, clock, ids = constructed
    persisted = persist_brief(s, clock)
    s.commit()
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    from atlas.api.main import app
    try:
        client = TestClient(app)
        r = client.get("/v1/reporting/brief/latest")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"session_date", "payload", "created_at",
                             "updated_at"}
        assert body["session_date"] == "2026-07-15"
        assert body["payload"] == persisted.payload
        assert body["payload"]["flags"]["billing_outage_suspected"] is True
        assert (body["payload"]["queue"]["proposals"][0]["id"]
                == ids["core_pid"])

        s.execute(text("DELETE FROM reporting.morning_brief"))
        s.commit()
        r = client.get("/v1/reporting/brief/latest")
        assert r.status_code == 404
        assert "no morning brief" in r.json()["detail"]
    finally:
        _clean(s)
        s.execute(text("TRUNCATE audit.decision_events RESTART IDENTITY"))
        s.commit()
        reset_app_engine()


def test_billing_signature_heuristic_truth_table(clean_audit, monkeypatch):
    """The brief's OWN signature (independent of the detector's alert): desk
    spend $0.00 with zero memos on a night the desk was EXPECTED to produce.
    A desk that was off, or that spent/memo'd, is not a suspected outage."""
    monkeypatch.setenv("ATLAS_DAILY_LLM_BUDGET_USD", "10.0")
    monkeypatch.delenv("ATLAS_BUDGET_NIGHTLY", raising=False)
    s = clean_audit
    _clean(s)
    clock = FrozenClock(NOW)
    run_id = f"daily-{DAY.isoformat()}"
    s.execute(text(
        "INSERT INTO workflow.workflow_runs (run_id, started_at, status) "
        "VALUES (:r, :t, 'completed')"), {"r": run_id, "t": NOW})

    # desk OFF -> not expected -> no suspicion (weekends must stay quiet)
    _node(s, run_id, "t7_desk", "done", "desk off (no model key configured)",
          NOW - timedelta(minutes=20))
    assert assemble_brief(s, clock).payload["flags"][
        "billing_outage_suspected"] is False

    # desk RAN and produced nothing for $0.00 -> the signature fires
    s.execute(text("UPDATE workflow.workflow_node_results "
                   "SET output_ref = :ref WHERE run_id = :r"),
              {"ref": T7_LINE, "r": run_id})
    assert assemble_brief(s, clock).payload["flags"][
        "billing_outage_suspected"] is True

    # a completed (even failed-schema) run row = real spend surface -> the
    # $0.00-with-memos-expected signature needs BOTH zeros; give it a memo
    s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        " recommendation, evidence_refs, created_at) "
        "VALUES ('committee', 'ZBRF', 'BUY', '[]', :t)"), {"t": NOW})
    assert assemble_brief(s, clock).payload["flags"][
        "billing_outage_suspected"] is False
