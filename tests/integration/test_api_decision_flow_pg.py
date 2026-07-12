"""Decision flow (Research page viewer): one memo's journey, stage by stage.

Builds the FULL story on atlas_test through the real seams — a scanner.completed
audit event shortlisting the symbol, a committee memo with its 0013 evidence
rows, a bridged proposal via build_proposal, the human seal over HTTP — then
pins GET /v1/research/memos/{id}/decision-flow: all six stages' shapes, the
honest fallbacks for memos that predate 0013 / were never scanned / never
bridged, the recorded bridge-skip reason verbatim, and the 404 envelope.

Timestamps are fully injected (memo created_at is written explicitly; audit
events ride FrozenClocks) so nothing here couples to the wall clock.
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
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.bridge import bridge_memos
from atlas.dcp.trading.proposals import build_proposal
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)   # limit set v1 first effective day

EVIDENCE = [
    ("dcp:bars:ZFLW:2026-07-13", "ZFLW daily closes: flat tape at the cited date"),
    ("dcp:indicators:ZFLW:2026-07-13", "trend indicators computed by the DCP"),
    ("quant:report:momentum-v1:ZFLW", "no validated strategy covers this name"),
]


def _wipe(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE source = 'flow-test'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZFLW%')"))
    s.execute(text("DELETE FROM research.memo_evidence WHERE memo_id IN "
                   "(SELECT id FROM research.memos WHERE instrument_symbol LIKE 'ZFLW%')"))
    s.execute(text("DELETE FROM research.memos WHERE instrument_symbol LIKE 'ZFLW%'"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZFLW%'"))


def _memo(s, symbol: str, *, run_id=None, refs=None, dissent="bear: unvalidated name",
          debate_summary="bull momentum vs bear validation gap — watch prevailed",
          created_at=None) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (agent_run_id, memo_type, instrument_symbol, "
        "recommendation, conviction, thesis, kill_criteria, evidence_refs, "
        "dissent, debate_summary, created_at) "
        "VALUES (:run, 'committee', :sym, 'BUY', 'MEDIUM', 'trend intact; size it', "
        "        '[\"trend breaks\", \"gates keep failing\"]', CAST(:er AS jsonb), "
        "        :d, :ds, :ca) RETURNING id"),
        {"run": run_id, "sym": symbol,
         "er": json.dumps(refs if refs is not None else [r for r, _ in EVIDENCE]),
         "d": dissent, "ds": debate_summary,
         "ca": created_at or (T0 - timedelta(hours=1))}).scalar())


@pytest.fixture
def fclient(monkeypatch, clean_audit):
    """The full story: scanner event -> memo + evidence rows -> bridged
    proposal (build_proposal) -> approval over HTTP, under injected clocks."""
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    clock = FrozenClock(T0)
    monkeypatch.setattr(trading_router, "_clock", lambda: clock)

    s = clean_audit
    _wipe(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) VALUES ('ZFLW', 'XTEST', 'US', 'stock', "
        "'ZFLW', 'Information Technology', 'USD') RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, close, volume, source) "
        "VALUES (:iid, :d, 100, 100, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)} for i in range(21)])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2026-07-10', 1.5, 'flow-test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = 1.5"))

    # the scan that routed the desk here — payload shaped exactly like scanner v1
    PostgresAuditLog(s, FrozenClock(T0 - timedelta(hours=2))).append(
        event_type="scanner.completed", entity_type="scanner",
        entity_id="2026-07-13", actor_type="dcp", actor_id="scanner_v1",
        payload={"criteria_version": "1.0", "top_n": 5,
                 "sessions": {"US": "2026-07-13"}, "scanned": 9, "eligible": 7,
                 "ineligible": 2,
                 "shortlist": [
                     {"symbol": "ZFLW", "held": False, "score": 1.75,
                      "ret20_abs": 0.21, "ret20_rank": 1.0,
                      "volume_surge": 1.4, "surge_rank": 0.75},
                     {"symbol": "ZOTHER", "held": True, "score": None,
                      "ret20_abs": None, "ret20_rank": None,
                      "volume_surge": None, "surge_rank": None}]})

    run_id = s.execute(text(
        "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, model, "
        "status, tokens_in, tokens_out, cost_usd, shadow) "
        "VALUES ('cio', 'abc123def4567890', 'claude-test-1', 'ok', 1200, 340, "
        "        0.0421, false) RETURNING id")).scalar()
    memo_id = _memo(s, "ZFLW", run_id=run_id)
    s.execute(text(
        "INSERT INTO research.memo_evidence (memo_id, ordinal, ref, body) "
        "VALUES (:m, :o, :ref, :body)"),
        [{"m": memo_id, "o": i, "ref": ref, "body": body}
         for i, (ref, body) in enumerate(EVIDENCE)])

    res = build_proposal(
        s, clock, memo_id=memo_id, symbol="ZFLW", signal_refs=[str(uuid4())],
        entry_price=Decimal("100"), stop_price=Decimal("95"),
        target_price=Decimal("120"))
    assert res.state == "pending_approval"
    s.commit()

    client = TestClient(app)
    clock.advance_to(T0 + timedelta(hours=1))
    approved = client.post(f"/v1/trading/proposals/{res.proposal_id}/approve",
                           json={"acknowledged_risks": True})
    assert approved.status_code == 200

    yield client, s, clock, memo_id, res
    _wipe(s)
    s.commit()
    reset_app_engine()


def test_full_story_pins_all_six_stages(fclient):
    c, _, _, memo_id, res = fclient
    r = c.get(f"/v1/research/memos/{memo_id}/decision-flow")
    assert r.status_code == 200
    d = r.json()
    assert d["memo_id"] == memo_id and d["symbol"] == "ZFLW"
    stages = d["stages"]

    # --- SCANNER: the nearest prior scan that shortlisted the symbol
    sc = stages["scanner"]
    assert sc["available"] is True
    assert sc["criteria_version"] == "1.0"
    assert (sc["scanned"], sc["eligible"], sc["top_n"]) == (9, 7, 5)
    assert sc["scanned_at"].startswith("2026-07-13T18:00")
    assert sc["entry"] == {"symbol": "ZFLW", "held": False, "score": 1.75,
                           "ret20_abs": 0.21, "ret20_rank": 1.0,
                           "volume_surge": 1.4, "surge_rank": 0.75}

    # --- EVIDENCE: 0013 rows verbatim, ordinal order
    ev = stages["evidence"]
    assert ev["available"] is True
    assert [(x["ordinal"], x["ref"], x["body"]) for x in ev["items"]] == [
        (i, ref, body) for i, (ref, body) in enumerate(EVIDENCE)]

    # --- DEBATE: the memo's own verbatim fields
    db = stages["debate"]
    assert db["available"] is True
    assert db["debate_summary"].startswith("bull momentum vs bear")
    assert db["dissent"] == "bear: unvalidated name"

    # --- VERDICT: recommendation + the flight-recorder line
    v = stages["verdict"]
    assert (v["recommendation"], v["conviction"]) == ("BUY", "MEDIUM")
    assert v["thesis"] == "trend intact; size it"
    assert v["kill_criteria"] == ["trend breaks", "gates keep failing"]
    assert v["evidence_refs"] == [ref for ref, _ in EVIDENCE]
    run = v["agent_run"]
    assert (run["model"], run["status"], run["shadow"]) == ("claude-test-1", "ok", False)
    assert run["cost_usd"] == pytest.approx(0.0421)

    # --- BRIDGE: the deterministic proposal, ADR-0006 numbers as recorded
    br = stages["bridge"]
    assert br["available"] is True
    p = br["proposal"]
    assert p["id"] == res.proposal_id
    assert p["state"] == "approved" and p["action"] == "buy"
    assert p["qty"] == 53                              # L1 binds: 8% NAV / A$150
    assert float(p["position_value_aud"]) == 7950.0
    assert float(p["entry_price"]) == 100
    assert float(p["stop_loss"]) == 95
    assert float(p["target_price"]) == 120

    # --- SEAL: the human decision on the bridged proposal
    se = stages["seal"]
    assert se["available"] is True
    assert (se["state"], se["status"]) == ("approved", "approved")
    assert len(se["approvals"]) == 1
    a = se["approvals"][0]
    assert (a["decision"], a["approver"], a["auth_method"]) == \
        ("approve", "principal", "console")
    assert a["decided_at"].startswith("2026-07-13T21:00")


def test_pre_0013_memo_answers_honest_fallbacks(fclient):
    """A memo that predates the feature (no evidence rows), was never
    shortlisted, never debated and never bridged: every gap is an honest
    note, never a reconstruction."""
    c, s, _, _, _ = fclient
    memo_id = _memo(s, "ZFLW2", refs=[], dissent="", debate_summary="")
    s.commit()
    stages = c.get(f"/v1/research/memos/{memo_id}/decision-flow").json()["stages"]

    assert stages["scanner"] == {"available": False,
                                 "note": "pre-scanner memo or not shortlisted"}
    assert stages["evidence"] == {
        "available": False,
        "note": "evidence bodies not recorded before this feature"}
    assert stages["debate"]["available"] is False
    assert stages["verdict"]["available"] is True      # the memo itself IS the record
    assert stages["verdict"]["agent_run"] is None      # hand-authored: no run row
    assert stages["bridge"] == {"available": False, "note": "not bridged"}
    assert stages["seal"] == {"available": False,
                              "note": "no proposal — nothing reached the seal"}


def test_bridge_skip_reason_surfaces_verbatim(fclient):
    """A memo the bridge skipped shows the RECORDED skip reason from the
    trading.bridge.completed payload — the skip is an honest outcome."""
    c, s, clock, _, _ = fclient
    memo_id = _memo(s, "ZFLWX")        # no such instrument -> recorded skip
    report = bridge_memos(s, clock)
    s.commit()
    assert memo_id in {k.memo_id for k in report.skipped}

    br = c.get(f"/v1/research/memos/{memo_id}/decision-flow").json()["stages"]["bridge"]
    assert br["available"] is False
    assert br["note"] == ("bridge skipped: expected exactly one active "
                          "instrument for 'ZFLWX', found 0")


def test_unknown_memo_answers_the_envelope(fclient):
    c, _, _, _, _ = fclient
    missing = c.get(f"/v1/research/memos/{uuid4()}/decision-flow")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    garbage = c.get("/v1/research/memos/not-a-uuid/decision-flow")
    assert garbage.status_code == 404
    assert garbage.json()["error"]["code"] == "NOT_FOUND"
