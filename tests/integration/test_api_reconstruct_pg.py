"""Decision reconstruction (Doc 01 §8 / Doc 06 §2): the full lineage tree.

Builds a REAL lifecycle through the same seams the console uses — limit set,
ZREC instrument + bars + FX, committee memo (with agent run + Principal
review), build_proposal -> approve over HTTP -> settle at the next session's
open — then asserts GET /v1/audit/decisions/{id}/reconstruct returns the whole
tree top-down: proposal, memo lineage, both itemised risk checks, the human
seal, order -> execution -> tax lot, and the audit chain's own account of the
decision in seq order. A risk-FAIL proposal reconstructs too: its FAIL check,
no orders, no approvals — honest failures are part of the record.
"""
from __future__ import annotations

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
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE source = 'recon-test'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZREC%')"))
    s.execute(text("DELETE FROM research.memo_reviews WHERE memo_id IN "
                   "(SELECT id FROM research.memos WHERE instrument_symbol LIKE 'ZREC%')"))
    s.execute(text("DELETE FROM research.memos WHERE instrument_symbol LIKE 'ZREC%'"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZREC%'"))


def _instrument(s, symbol: str, *, sector: str = "Information Technology"):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, :sec, 'USD') RETURNING id"),
        {"sym": symbol, "sec": sector}).scalar()


def _bars(s, iid, days: list[date], *, close: Decimal) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": close} for d in days])


def _fx(s, *, day: date) -> None:
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', :d, 1.5, 'recon-test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = 1.5"),
        {"d": day})


def _memo(s, symbol: str, *, thesis: str, run_id=None) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (agent_run_id, memo_type, instrument_symbol, "
        "recommendation, conviction, thesis, kill_criteria, evidence_refs, "
        "dissent, debate_summary) "
        "VALUES (:run, 'committee', :sym, 'BUY', 'MEDIUM', :thesis, "
        "        '[\"thesis broken\", \"regime flips\"]', '[]', "
        "        'bear: crowded trade', 'bull b1 vs bear b2 — bull prevailed') "
        "RETURNING id"), {"run": run_id, "sym": symbol, "thesis": thesis}).scalar())


@pytest.fixture
def rclient(monkeypatch, clean_audit):
    """Full REAL lifecycle to reconstruct: build -> approve (HTTP) -> settle
    (HTTP) with the next session's bar + FX via the FrozenClock seam."""
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    clock = FrozenClock(T0)
    monkeypatch.setattr(trading_router, "_clock", lambda: clock)

    s = clean_audit
    _wipe(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = _instrument(s, "ZREC")
    _bars(s, iid, [date(2026, 6, 23) + timedelta(days=i) for i in range(21)],
          close=Decimal("100"))
    _fx(s, day=date(2026, 7, 10))
    run_id = s.execute(text(
        "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, model, "
        "status, tokens_in, tokens_out, cost_usd, shadow) "
        "VALUES ('cio', 'abc123def4567890', 'claude-test-1', 'ok', 1200, 340, "
        "        0.0421, false) RETURNING id")).scalar()
    memo_id = _memo(s, "ZREC", thesis="momentum regime intact; entry at support",
                    run_id=run_id)
    s.execute(text("INSERT INTO research.memo_reviews (memo_id, verdict, notes) "
                   "VALUES (:m, 'agree', 'sound reasoning')"), {"m": memo_id})
    res = build_proposal(
        s, clock, memo_id=memo_id, symbol="ZREC", signal_refs=[str(uuid4())],
        entry_price=Decimal("100"), stop_price=Decimal("95"),
        target_price=Decimal("120"))
    assert res.state == "pending_approval"
    s.commit()

    client = TestClient(app)
    clock.advance_to(T0 + timedelta(hours=1))
    approved = client.post(f"/v1/trading/proposals/{res.proposal_id}/approve",
                           json={"acknowledged_risks": True})
    assert approved.status_code == 200

    # next session's bar AND its FX rate arrive -> the paper fill can happen
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        "volume, source) VALUES (:iid, :d, 102, 103, 1000000, 'EodhdAdapter')"),
        {"iid": iid, "d": NEXT_SESSION})
    _fx(s, day=NEXT_SESSION)
    s.commit()
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    settled = client.post("/v1/trading/settle").json()
    assert len(settled["fills"]) == 1

    yield client, s, clock, res
    _wipe(s)
    s.commit()
    reset_app_engine()


def test_executed_lifecycle_reconstructs_full_tree(rclient):
    c, _, _, res = rclient
    r = c.get(f"/v1/audit/decisions/{res.proposal_id}/reconstruct")
    assert r.status_code == 200
    t = r.json()

    # --- proposal: the full row, symbol/market via instruments
    p = t["proposal"]
    assert p["id"] == res.proposal_id
    assert p["state"] == "executed"
    assert (p["symbol"], p["market"], p["action"]) == ("ZREC", "US", "buy")
    assert float(p["entry_price"]) == 100 and float(p["stop_loss"]) == 95
    assert float(p["target_price"]) == 120
    assert p["position_size"] == 53                    # L1 binds: 8% of A$100k / A$150
    assert float(p["position_value_aud"]) == 7950.0
    assert p["committee_memo_id"] and p["signal_ids"]
    assert p["expires_at"] and p["created_at"]

    # --- memo lineage: thesis, review, agent run
    m = t["memo"]
    assert m["type"] == "committee" and m["symbol"] == "ZREC"
    assert m["thesis"] == "momentum regime intact; entry at support"
    assert (m["recommendation"], m["conviction"]) == ("BUY", "MEDIUM")
    assert m["kill_criteria"] == ["thesis broken", "regime flips"]
    assert m["dissent"] == "bear: crowded trade"
    assert m["review"]["verdict"] == "agree"
    assert m["review"]["notes"] == "sound reasoning"
    run = m["agent_run"]
    assert (run["model"], run["status"], run["shadow"]) == ("claude-test-1", "ok", False)
    assert (run["tokens_in"], run["tokens_out"]) == (1200, 340)
    assert run["cost_usd"] == pytest.approx(0.0421)
    assert run["template_hash_prefix"] == "abc123def4"   # left(hash, 10)

    # --- exactly two checks, chronological: proposal then the fresh §2.2 one
    checks = t["risk_checks"]
    assert [x["kind"] for x in checks] == ["proposal", "approval_time"]
    assert all(x["verdict"] == "PASS" for x in checks)
    assert all(x["limit_set_version"] == 1 for x in checks)
    for x in checks:
        l1 = next(row for row in x["results"] if row["rule"] == "L1")
        assert l1["pass"] is True and l1["value"] is not None and l1["limit"] == 0.08
        assert x["price_snapshot"]["nav_aud"]           # jsonb kept as stored

    # --- one human seal, referencing the approval-time check
    assert len(t["approvals"]) == 1
    a = t["approvals"][0]
    assert (a["decision"], a["approver"], a["auth_method"]) == \
        ("approve", "principal", "console")
    assert a["approval_time_risk_check_id"] == checks[1]["id"]
    assert a["decided_at"]

    # --- order -> execution (golden fill) -> tax lot
    assert len(t["orders"]) == 1
    o = t["orders"][0]
    assert (o["state"], o["side"], o["qty"]) == ("filled", "buy", 53)
    assert o["broker"] == "paper" and o["created_at"]
    assert len(o["executions"]) == 1
    e = o["executions"][0]
    assert e["fill_qty"] == 53
    assert float(e["decision_price"]) == 100            # Doc 04 §14
    assert float(e["fill_price"]) == pytest.approx(102.102)
    assert float(e["shortfall_bps"]) == pytest.approx(210.2)
    assert float(e["fx_rate_used"]) == 1.5
    assert e["executed_at"].startswith("2026-07-14T13:30")   # XNYS open
    assert len(e["tax_lots"]) == 1
    lot = e["tax_lots"][0]
    assert lot["qty"] == 53
    assert float(lot["cost_aud"]) == pytest.approx(8117.11)  # 53 * 102.102 * 1.5
    assert lot["acquired_at"] and lot["disposed_at"] is None
    assert lot["proceeds_aud"] is None

    # --- the chain's own account, in seq order (build emits the check event
    # before proposal.created; settle opens the position before recording)
    evs = [x["event_type"] for x in t["events"]]
    assert evs == ["risk.check.completed", "proposal.created",
                   "risk.check.completed", "proposal.approved",
                   "order.state_changed",                    # -> pending_submit
                   "position.opened", "execution.recorded",
                   "order.state_changed",                    # -> filled
                   "proposal.executed"]
    seqs = [x["seq"] for x in t["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    ex_ev = next(x for x in t["events"] if x["event_type"] == "execution.recorded")
    assert ex_ev["payload"]["shortfall_bps"] == "210.2000"   # payload verbatim
    assert ex_ev["actor_type"] == "broker"
    appr_ev = next(x for x in t["events"] if x["event_type"] == "proposal.approved")
    assert appr_ev["actor_type"] == "human" and appr_ev["actor_id"] == "principal"

    # --- unknown ids answer the §3.3 envelope, not a bare detail or a 500
    missing = c.get(f"/v1/audit/decisions/{uuid4()}/reconstruct")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    garbage = c.get("/v1/audit/decisions/not-a-uuid/reconstruct")
    assert garbage.status_code == 404
    assert garbage.json()["error"]["code"] == "NOT_FOUND"


def test_rejected_risk_fail_reconstructs_with_fail_check_and_no_orders(rclient):
    """A risk FAIL is terminal and honest — the tree must show the FAIL check
    itemised, no approvals, no orders, and ONLY this decision's events."""
    c, s, clock, _ = rclient
    # Existing correlated holding: ZREC2 has only 5 sessions of history, so the
    # L8 correlation feed fails CLOSED to 1; combined weight busts the cluster cap.
    zid = _instrument(s, "ZREC2", sector="Financials")
    _bars(s, zid, [date(2026, 7, 6) + timedelta(days=i) for i in range(5)],
          close=Decimal("100"))
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at, current_stop) VALUES (:iid, 70, 100, 'USD', :t, 90)"),
        {"iid": zid, "t": datetime(2026, 7, 10, 15, 0, tzinfo=UTC)})
    z3 = _instrument(s, "ZREC3")
    _bars(s, z3, [date(2026, 6, 23) + timedelta(days=i) for i in range(21)],
          close=Decimal("100"))
    memo_id = _memo(s, "ZREC3", thesis="second thesis — never gets its seal")
    res = build_proposal(
        s, clock, memo_id=memo_id, symbol="ZREC3", signal_refs=[str(uuid4())],
        entry_price=Decimal("100"), stop_price=Decimal("95"),
        target_price=Decimal("120"))
    assert res.state == "rejected" and res.verdict == "FAIL"
    assert "L8" in res.failures
    s.commit()

    t = c.get(f"/v1/audit/decisions/{res.proposal_id}/reconstruct").json()
    assert t["proposal"]["state"] == "rejected"
    assert t["proposal"]["symbol"] == "ZREC3"
    assert t["proposal"]["risk_check_id"] is None    # §2.1: only a PASS is referenced
    assert t["memo"]["thesis"] == "second thesis — never gets its seal"
    assert t["memo"]["review"] is None and t["memo"]["agent_run"] is None

    assert len(t["risk_checks"]) == 1
    chk = t["risk_checks"][0]
    assert (chk["verdict"], chk["kind"]) == ("FAIL", "proposal")
    l8 = next(row for row in chk["results"] if row["rule"] == "L8")
    assert l8["pass"] is False and l8["limit"] is not None

    assert t["approvals"] == [] and t["orders"] == []
    # only THIS decision's events — the executed ZREC lifecycle stays out
    evs = [x["event_type"] for x in t["events"]]
    assert evs == ["risk.check.completed", "proposal.created"]
    assert all(x["payload"]["proposal_id"] == res.proposal_id for x in t["events"])
