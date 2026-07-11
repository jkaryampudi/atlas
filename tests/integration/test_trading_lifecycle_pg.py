"""Phase-5 paper-trading lifecycle (Doc 05 §5, Doc 06 §3, Doc 04 §2/§4/§14).

Exercises the full deterministic loop against atlas_test: build -> approve
(with the Doc 04 §2.2 approval-time re-check) -> settle at the next session's
open via the PaperBroker -> snapshot; plus every terminal path (risk FAIL,
expiry, re-check void, reject), settle idempotency, the overnight
pending_submit state, and the Doc 05 §7 enforcement columns.

Nothing is committed: the pg_session fixture rolls back, so atlas_test keeps
none of the trading rows created here. Defensive deletes at seed time guard
against debris from previously crashed runs.
"""
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.proposals import (
    SEED_CASH_AUD,
    approve,
    build_proposal,
    cancel_order,
    expire_stale,
    reject,
    settle_orders,
    snapshot,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
# Monday 2026-07-13: the first day limit set v1 is effective (seeds/limit_set_v1.json)
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
NEXT_SESSION = date(2026, 7, 14)  # XNYS session after 2026-07-13
FX_USD_AUD = Decimal("1.5")


# ------------------------------------------------------------------- seeding

def _clean_trading(s) -> None:
    """Remove any committed debris from crashed runs (FK-safe order)."""
    # leave pending_approval too: the §2.1 CHECK forbids it without a check ref
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZTL%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZTL%'"))


def _instrument(s, symbol: str, *, sector: str = "Information Technology"):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, :sec, 'USD') RETURNING id"),
        {"sym": symbol, "sec": sector}).scalar()


def _bars(s, iid, days: list[date], *, close: Decimal, volume: int = 1_000_000) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, close, volume, source) "
        "VALUES (:iid, :d, :o, :c, :v, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "o": close, "c": close, "v": volume} for d in days])


def _fx(s, *, day: date, rate: Decimal = FX_USD_AUD) -> None:
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', :d, :r, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"d": day, "r": rate})


def _memo(s) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (memo_type, recommendation, evidence_refs) "
        "VALUES ('committee', 'BUY', '[]') RETURNING id")).scalar())


def _seed(s) -> str:
    """Baseline market state: candidate ZTLA with 21 sessions of history and a
    same-day USD->AUD rate. Returns the committee memo id."""
    _clean_trading(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = _instrument(s, "ZTLA")
    _bars(s, iid, [date(2026, 6, 23) + timedelta(days=i) for i in range(21)],
          close=Decimal("100"))
    _fx(s, day=date(2026, 7, 10))
    return _memo(s)


def _build(s, clock, memo_id: str):
    return build_proposal(
        s, clock, memo_id=memo_id, symbol="ZTLA", signal_refs=[str(uuid4())],
        entry_price=Decimal("100"), stop_price=Decimal("95"),
        target_price=Decimal("120"))


def _events(s) -> list[str]:
    return [r[0] for r in s.execute(text(
        "SELECT event_type FROM audit.decision_events ORDER BY seq")).all()]


# ---------------------------------------------------------------- happy path

def test_full_happy_path_build_approve_settle_snapshot(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)

    # --- build: sized by risk (L1 binds: 8% of A$100k NAV / A$150 = 53 shares)
    res = _build(s, clock, memo_id)
    assert res.state == "pending_approval"
    assert res.verdict == "PASS"
    assert res.qty == 53
    row = s.execute(text(
        "SELECT state, expires_at, position_size, position_value_aud, risk_check_id, action "
        "FROM trading.trade_proposals WHERE id = :p"),
        {"p": res.proposal_id}).one()
    assert row.state == "pending_approval"
    assert row.action == "buy"
    assert row.expires_at == T0 + timedelta(hours=24)          # 24h TTL
    assert row.position_size == 53
    assert row.position_value_aud == Decimal("7950.00")        # 53 * 100 * 1.5
    assert str(row.risk_check_id) == res.risk_check_id         # §2.1: PASS referenced
    check = s.execute(text(
        "SELECT verdict, check_kind, limit_set_version, price_snapshot "
        "FROM risk.risk_checks WHERE id = :c"), {"c": res.risk_check_id}).one()
    assert (check.verdict, check.check_kind, check.limit_set_version) == ("PASS", "proposal", 1)
    # empty book: the pro-forma NAV is the documented A$100k paper seed
    assert Decimal(check.price_snapshot["nav_aud"]) == SEED_CASH_AUD

    # --- approve: fresh re-check (Doc 04 §2.2), approvals row, order pending_submit
    clock.advance_to(T0 + timedelta(hours=1))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"
    assert outcome.order_id is not None
    assert outcome.risk_check_id != res.risk_check_id          # a FRESH check, not reuse
    appr = s.execute(text(
        "SELECT decision, approver, auth_method, approval_time_risk_check_id "
        "FROM trading.approvals WHERE proposal_id = :p"), {"p": res.proposal_id}).one()
    assert (appr.decision, appr.approver, appr.auth_method) == ("approve", "principal", "console")
    assert str(appr.approval_time_risk_check_id) == outcome.risk_check_id
    order = s.execute(text(
        "SELECT state, side, qty, approval_id, risk_check_id FROM trading.orders "
        "WHERE id = :o"), {"o": outcome.order_id}).one()
    assert (order.state, order.side, order.qty) == ("pending_submit", "buy", 53)
    assert str(order.risk_check_id) == outcome.risk_check_id   # Doc 05 §7 lineage

    # --- overnight: tomorrow's open is unknown -> the order stays pending_submit
    clock.advance_to(T0 + timedelta(hours=2))
    assert settle_orders(s, clock) == ()
    state = s.execute(text("SELECT state FROM trading.orders WHERE id = :o"),
                      {"o": outcome.order_id}).scalar()
    assert state == "pending_submit"

    # --- next session's bar AND its FX rate arrive: fill at the session open
    # with CostModel bps applied (the FX gate mirrors the bar gate)
    iid = s.execute(text("SELECT id FROM market.instruments WHERE symbol = 'ZTLA'")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        "volume, source) VALUES (:iid, :d, 102, 103, 1000000, 'EodhdAdapter')"),
        {"iid": iid, "d": NEXT_SESSION})
    _fx(s, day=NEXT_SESSION)
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    fills = settle_orders(s, clock)
    assert len(fills) == 1
    assert fills[0].order_id == outcome.order_id
    assert fills[0].fill_date == NEXT_SESSION
    assert fills[0].fill_price == Decimal("102.102000")        # 102 * (1 + 10bps)
    assert fills[0].shortfall_bps == Decimal("210.2000")       # vs decision price 100
    ex = s.execute(text(
        "SELECT fill_qty, fill_price, fees, fx_rate_used, decision_price, shortfall_bps, "
        "executed_at FROM trading.executions WHERE order_id = :o"),
        {"o": outcome.order_id}).one()
    assert (ex.fill_qty, ex.fees) == (53, Decimal(0))
    assert ex.fill_price == Decimal("102.102000")
    assert ex.fx_rate_used == FX_USD_AUD
    assert ex.decision_price == Decimal("100.000000")          # Doc 04 §14
    assert ex.shortfall_bps == Decimal("210.2000")
    assert ex.executed_at == datetime(2026, 7, 14, 13, 30, tzinfo=UTC)  # XNYS open
    pos = s.execute(text(
        "SELECT qty, avg_cost, currency, current_stop FROM trading.positions "
        "WHERE closed_at IS NULL")).one()
    assert (pos.qty, pos.currency) == (53, "USD")
    assert pos.avg_cost == Decimal("102.102000")
    assert pos.current_stop == Decimal("95.000000")
    lot = s.execute(text("SELECT qty, cost_aud FROM trading.tax_lots")).one()
    assert (lot.qty, lot.cost_aud) == (53, Decimal("8117.11"))  # 53 * 102.102 * 1.5
    states = s.execute(text(
        "SELECT o.state, tp.state FROM trading.orders o "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id")).one()
    assert states == ("filled", "executed")

    # --- settle is idempotent: nothing pending, no second execution
    assert settle_orders(s, clock) == ()
    n_ex = s.execute(text("SELECT count(*) FROM trading.executions")).scalar()
    assert n_ex == 1

    # --- snapshot: positions + latest closes + FX -> compute_snapshot, persisted
    clock.advance_to(datetime(2026, 7, 14, 22, 30, tzinfo=UTC))
    snap = snapshot(s, clock)
    # cash: A$100k seed - 53 * 102.102 * 1.5 = 91882.891; NAV adds 53*103*1.5
    assert snap.cash_aud == Decimal("91882.89")
    assert snap.nav_aud == Decimal("100071.39")
    assert snap.open_risk_pct == Decimal("0.0064")             # (103-95)*53*1.5 / NAV
    stored = s.execute(text(
        "SELECT nav_aud, cash_aud, holdings, open_risk_pct FROM trading.portfolio_snapshots "
        "WHERE id = :i"), {"i": snap.snapshot_id}).one()
    assert (stored.nav_aud, stored.cash_aud) == (snap.nav_aud, snap.cash_aud)
    assert stored.holdings[0]["symbol"] == "ZTLA"

    # --- every material action hit the audit chain
    evs = _events(s)
    assert evs.count("risk.check.completed") == 2              # proposal + approval_time
    for ev in ("proposal.created", "proposal.approved", "execution.recorded",
               "proposal.executed", "portfolio.snapshot.created"):
        assert evs.count(ev) == 1, ev
    assert evs.count("order.state_changed") == 2               # -> pending_submit, -> filled


# ------------------------------------------------------------- terminal paths

def test_risk_fail_lands_rejected_and_is_terminal(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    # Existing correlated holding: ZTLB has only 5 sessions of history, so the
    # L8 correlation feed fails CLOSED to 1; combined weight ~17% > 12% cap.
    zid = _instrument(s, "ZTLB", sector="Financials")
    _bars(s, zid, [date(2026, 7, 6) + timedelta(days=i) for i in range(5)],
          close=Decimal("100"))
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at, current_stop) "
        "VALUES (:iid, 70, 100, 'USD', :t, 90)"),
        {"iid": zid, "t": datetime(2026, 7, 10, 15, 0, tzinfo=UTC)})
    clock = FrozenClock(T0)

    res = _build(s, clock, memo_id)
    assert res.state == "rejected"
    assert res.verdict == "FAIL"
    assert "L8" in res.failures
    row = s.execute(text(
        "SELECT state, risk_check_id FROM trading.trade_proposals WHERE id = :p"),
        {"p": res.proposal_id}).one()
    assert row.state == "rejected"
    assert row.risk_check_id is None                 # only a PASS is referenced (§2.1)
    check = s.execute(text(
        "SELECT verdict, check_kind FROM risk.risk_checks WHERE proposal_id = :p"),
        {"p": res.proposal_id}).one()
    assert (check.verdict, check.check_kind) == ("FAIL", "proposal")
    assert "proposal.created" in _events(s) and "risk.check.completed" in _events(s)
    # risk FAIL is terminal: there is no approval path out of 'rejected'
    with pytest.raises(ValueError, match="pending_approval"):
        approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)


def test_expire_stale_transitions_and_audits(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    res = _build(s, clock, memo_id)

    clock.advance_to(T0 + timedelta(hours=23))
    assert expire_stale(s, clock) == ()              # not yet past the 24h TTL
    clock.advance_to(T0 + timedelta(hours=25))
    assert expire_stale(s, clock) == (res.proposal_id,)
    state = s.execute(text("SELECT state FROM trading.trade_proposals WHERE id = :p"),
                      {"p": res.proposal_id}).scalar()
    assert state == "expired"
    assert _events(s).count("proposal.expired") == 1
    assert expire_stale(s, clock) == ()              # idempotent


def test_approve_expired_returns_proposal_expired(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    res = _build(s, clock, memo_id)

    clock.advance_to(T0 + timedelta(hours=25))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "PROPOSAL_EXPIRED"      # Doc 06 §3.3 error code
    assert outcome.order_id is None
    state = s.execute(text("SELECT state FROM trading.trade_proposals WHERE id = :p"),
                      {"p": res.proposal_id}).scalar()
    assert state == "expired"
    assert s.execute(text("SELECT count(*) FROM trading.orders")).scalar() == 0
    assert "proposal.expired" in _events(s)


def test_approval_recheck_failure_voids_without_raising(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    res = _build(s, clock, memo_id)
    assert res.verdict == "PASS"

    # Mutate the world between build and approve: a tighter limit set (v2)
    # becomes active, so the FRESH check fails L1 on the same proposal.
    limits_v2 = s.execute(text("SELECT limits FROM risk.limit_sets WHERE version = 1")).scalar()
    limits_v2 = dict(limits_v2)
    limits_v2["L1_max_stock_weight"] = 0.005
    s.execute(text(
        "INSERT INTO risk.limit_sets (version, mode, limits, effective_from, created_by, "
        "confirmation_a, confirmation_b) "
        "VALUES (2, 'small_aum', CAST(:l AS jsonb), :ef, 'principal:test', "
        "        :t - interval '2 hours', :t)"),
        {"l": json.dumps(limits_v2), "ef": date(2026, 7, 13), "t": T0})

    clock.advance_to(T0 + timedelta(hours=1))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "RISK_RECHECK_FAILED"   # structured result, not an exception
    assert outcome.order_id is None
    assert any(f.startswith("L1") for f in outcome.failures)
    row = s.execute(text("SELECT state FROM trading.trade_proposals WHERE id = :p"),
                    {"p": res.proposal_id}).scalar()
    assert row == "voided"
    fresh = s.execute(text(
        "SELECT verdict, check_kind FROM risk.risk_checks WHERE id = :c"),
        {"c": outcome.risk_check_id}).one()
    assert (fresh.verdict, fresh.check_kind) == ("FAIL", "approval_time")
    assert s.execute(text("SELECT count(*) FROM trading.approvals")).scalar() == 0
    assert s.execute(text("SELECT count(*) FROM trading.orders")).scalar() == 0
    assert "proposal.voided" in _events(s)


def test_reject_records_and_audits(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    res = _build(s, clock, memo_id)

    reject(s, clock, proposal_id=res.proposal_id, reason="thesis stale")
    state = s.execute(text("SELECT state FROM trading.trade_proposals WHERE id = :p"),
                      {"p": res.proposal_id}).scalar()
    assert state == "rejected"
    appr = s.execute(text(
        "SELECT decision, approval_time_risk_check_id FROM trading.approvals "
        "WHERE proposal_id = :p"), {"p": res.proposal_id}).one()
    assert appr.decision == "reject"
    assert str(appr.approval_time_risk_check_id) == res.risk_check_id
    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events WHERE event_type = 'proposal.rejected'")).one()
    assert ev.payload["reason"] == "thesis stale"
    with pytest.raises(ValueError, match="pending_approval"):
        reject(s, clock, proposal_id=res.proposal_id, reason="twice")


def test_no_next_session_bar_keeps_order_pending(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    res = _build(s, clock, memo_id)
    clock.advance_to(T0 + timedelta(hours=1))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"

    # Same evening AND the whole next day without a bar: still pending_submit —
    # the normal overnight state, never an error.
    for at in (T0 + timedelta(hours=3), datetime(2026, 7, 14, 23, 0, tzinfo=UTC)):
        clock.advance_to(at)
        assert settle_orders(s, clock) == ()
        state = s.execute(text("SELECT state FROM trading.orders WHERE id = :o"),
                          {"o": outcome.order_id}).scalar()
        assert state == "pending_submit"
    assert s.execute(text("SELECT count(*) FROM trading.executions")).scalar() == 0


def test_no_limit_set_before_effective_date_raises(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(datetime(2026, 7, 12, 20, 0, tzinfo=UTC))  # day before v1
    with pytest.raises(RuntimeError, match="no limit set"):
        _build(s, clock, memo_id)


# ------------------------------------------------- Doc 05 §7 schema enforcement

def test_enforcement_columns_reject_violations(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    expires = T0 + timedelta(hours=24)
    sig = [uuid4()]

    def rejects(sql: str, params: dict) -> None:
        with pytest.raises(IntegrityError):
            with s.begin_nested():
                s.execute(text(sql), params)

    # no trade without evidence: committee_memo_id / signal_ids NOT NULL,
    # and signal_ids must be NON-EMPTY (cardinality CHECK)
    rejects("INSERT INTO trading.trade_proposals (signal_ids, entry_price, stop_loss, "
            "target_price, state, expires_at) "
            "VALUES (:sig, 1, 1, 1, 'draft', :e)", {"sig": sig, "e": expires})
    rejects("INSERT INTO trading.trade_proposals (committee_memo_id, entry_price, stop_loss, "
            "target_price, state, expires_at) "
            "VALUES (:m, 1, 1, 1, 'draft', :e)", {"m": memo_id, "e": expires})
    rejects("INSERT INTO trading.trade_proposals (committee_memo_id, signal_ids, entry_price, "
            "stop_loss, target_price, state, expires_at) "
            "VALUES (:m, '{}', 1, 1, 1, 'draft', :e)", {"m": memo_id, "e": expires})
    # 24h TTL and the 8-state machine are structural
    rejects("INSERT INTO trading.trade_proposals (committee_memo_id, signal_ids, entry_price, "
            "stop_loss, target_price, state) "
            "VALUES (:m, :sig, 1, 1, 1, 'draft')", {"m": memo_id, "sig": sig})
    rejects("INSERT INTO trading.trade_proposals (committee_memo_id, signal_ids, entry_price, "
            "stop_loss, target_price, state, expires_at) "
            "VALUES (:m, :sig, 1, 1, 1, 'bogus', :e)",
            {"m": memo_id, "sig": sig, "e": expires})
    # Doc 04 §2.1 structurally: no pending_approval without a referenced check
    rejects("INSERT INTO trading.trade_proposals (committee_memo_id, signal_ids, entry_price, "
            "stop_loss, target_price, state, expires_at) "
            "VALUES (:m, :sig, 1, 1, 1, 'pending_approval', :e)",
            {"m": memo_id, "sig": sig, "e": expires})

    # a real proposal + PASS check to give the FK targets below
    pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (committee_memo_id, signal_ids, entry_price, "
        "stop_loss, target_price, state, expires_at) "
        "VALUES (:m, :sig, 1, 1, 1, 'draft', :e) RETURNING id"),
        {"m": memo_id, "sig": sig, "e": expires}).scalar()
    cid = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, results, verdict, check_kind) "
        "VALUES (:p, '[]', 'PASS', 'proposal') RETURNING id"), {"p": pid}).scalar()

    # approval requires the fresh approval-time check
    rejects("INSERT INTO trading.approvals (proposal_id, decision, approver) "
            "VALUES (:p, 'approve', 'principal')", {"p": pid})
    aid = s.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, "
        "approval_time_risk_check_id) "
        "VALUES (:p, 'approve', 'principal', :c) RETURNING id"), {"p": pid, "c": cid}).scalar()

    # no execution without risk approval: orders.approval_id + orders.risk_check_id
    rejects("INSERT INTO trading.orders (proposal_id, risk_check_id, state) "
            "VALUES (:p, :c, 'pending_submit')", {"p": pid, "c": cid})
    rejects("INSERT INTO trading.orders (proposal_id, approval_id, state) "
            "VALUES (:p, :a, 'pending_submit')", {"p": pid, "a": aid})
    oid = s.execute(text(  # fully-referenced order is accepted
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, state) "
        "VALUES (:p, :a, :c, 'pending_submit') RETURNING id"),
        {"p": pid, "a": aid, "c": cid}).scalar()

    # one full fill per order (v1): a second executions row is impossible
    s.execute(text("INSERT INTO trading.executions (order_id) VALUES (:o)"),
              {"o": oid})
    rejects("INSERT INTO trading.executions (order_id) VALUES (:o)", {"o": oid})

    # one OPEN position per instrument: the split-book race is unrepresentable
    iid = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'ZTLA'")).scalar()
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty) VALUES (:i, 1)"),
        {"i": iid})
    rejects("INSERT INTO trading.positions (instrument_id, qty) VALUES (:i, 1)",
            {"i": iid})

    # the NOT NULL declarations themselves, straight from the catalog
    cols = dict(s.execute(text(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'trading' AND table_name = 'orders'")).all())
    assert cols["approval_id"] == "NO" and cols["risk_check_id"] == "NO"


# --------------------------------------------- review-driven hardening tests

def _approved_order(s, clock, memo_id: str) -> tuple[str, str]:
    """build -> approve; returns (proposal_id, order_id)."""
    res = _build(s, clock, memo_id)
    clock.advance_to(clock.now() + timedelta(hours=1))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"
    assert outcome.order_id is not None
    return res.proposal_id, outcome.order_id


def _next_session_data(s) -> None:
    """The fill session's bar AND its FX rate (both gates open)."""
    iid = s.execute(text("SELECT id FROM market.instruments WHERE symbol = 'ZTLA'")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        "volume, source) VALUES (:iid, :d, 102, 103, 1000000, 'EodhdAdapter')"),
        {"iid": iid, "d": NEXT_SESSION})
    _fx(s, day=NEXT_SESSION)


def test_settle_refuses_tampered_lineage(clean_audit):
    """Doc 04 §2 item 3: the Execution service verifies the risk-check
    reference before submission — a pending order whose proposal is no longer
    'approved' is an integrity breach and must raise, never fill."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    proposal_id, _ = _approved_order(s, clock, memo_id)
    _next_session_data(s)
    s.execute(text(  # tamper behind the lifecycle's back
        "UPDATE trading.trade_proposals SET state = 'rejected' WHERE id = :p"),
        {"p": proposal_id})
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    with pytest.raises(RuntimeError, match="REFUSING to fill"):
        settle_orders(s, clock)
    assert s.execute(text("SELECT count(*) FROM trading.executions")).scalar() == 0


def test_double_fill_blocked_by_schema(clean_audit):
    """Even if application state is tampered back to pending, the UNIQUE
    index on executions(order_id) makes a second fill unrepresentable."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    proposal_id, order_id = _approved_order(s, clock, memo_id)
    _next_session_data(s)
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    assert len(settle_orders(s, clock)) == 1
    s.execute(text("UPDATE trading.orders SET state = 'pending_submit' WHERE id = :o"),
              {"o": order_id})
    s.execute(text("UPDATE trading.trade_proposals SET state = 'approved' WHERE id = :p"),
              {"p": proposal_id})
    with pytest.raises(IntegrityError):
        settle_orders(s, clock)


def test_cancel_order_voids_proposal_and_releases_capital(clean_audit):
    """A stuck pending_submit order (its session's bar never arrives) has a
    human escape hatch; afterwards the book no longer reserves its capital."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    proposal_id, order_id = _approved_order(s, clock, memo_id)

    cancel_order(s, clock, order_id=order_id, reason="fill-session bar never arrived")
    states = s.execute(text(
        "SELECT o.state, tp.state FROM trading.orders o "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "WHERE o.id = :o"), {"o": order_id}).one()
    assert states == ("cancelled", "voided")
    evs = _events(s)
    assert evs.count("proposal.voided") == 1
    assert "order.state_changed" in evs
    with pytest.raises(ValueError, match="not pending_submit"):
        cancel_order(s, clock, order_id=order_id, reason="twice")
    assert settle_orders(s, clock) == ()   # nothing pending anymore


def test_reject_expired_lands_expired_not_rejected(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    res = _build(s, clock, memo_id)
    clock.advance_to(T0 + timedelta(hours=25))
    outcome = reject(s, clock, proposal_id=res.proposal_id, reason="too late anyway")
    assert outcome.status == "PROPOSAL_EXPIRED"   # structured, so the commit holds
    state = s.execute(text("SELECT state FROM trading.trade_proposals WHERE id = :p"),
                      {"p": res.proposal_id}).scalar()
    assert state == "expired"
    assert s.execute(text("SELECT count(*) FROM trading.approvals")).scalar() == 0


def test_fill_waits_for_fill_date_fx(clean_audit):
    """The FX gate mirrors the bar gate: no fill-date rate, no fill — a stale
    weekend rate must never be baked into the immutable execution row."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _, order_id = _approved_order(s, clock, memo_id)
    iid = s.execute(text("SELECT id FROM market.instruments WHERE symbol = 'ZTLA'")).scalar()
    s.execute(text(  # bar arrives, FX job lags
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        "volume, source) VALUES (:iid, :d, 102, 103, 1000000, 'EodhdAdapter')"),
        {"iid": iid, "d": NEXT_SESSION})
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    assert settle_orders(s, clock) == ()
    state = s.execute(text("SELECT state FROM trading.orders WHERE id = :o"),
                      {"o": order_id}).scalar()
    assert state == "pending_submit"

    _fx(s, day=NEXT_SESSION)                # the rate lands -> the fill happens
    fills = settle_orders(s, clock)
    assert len(fills) == 1
    fx_used = s.execute(text(
        "SELECT fx_rate_used FROM trading.executions WHERE order_id = :o"),
        {"o": order_id}).scalar()
    assert fx_used == FX_USD_AUD            # the fill date's OWN rate


def test_no_intraday_fill_before_session_open(clean_audit):
    """Replay/live parity: over a fully backfilled DB, a clock stopped before
    the session open must not fill — executed_at can never be in the injected
    clock's future."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _, order_id = _approved_order(s, clock, memo_id)
    _next_session_data(s)                   # backfilled: bar + FX already present

    clock.advance_to(datetime(2026, 7, 14, 9, 0, tzinfo=UTC))   # pre-open
    assert settle_orders(s, clock) == ()

    clock.advance_to(datetime(2026, 7, 14, 14, 0, tzinfo=UTC))  # post-open
    fills = settle_orders(s, clock)
    assert len(fills) == 1
    executed_at = s.execute(text(
        "SELECT executed_at FROM trading.executions WHERE order_id = :o"),
        {"o": order_id}).scalar()
    assert executed_at == datetime(2026, 7, 14, 13, 30, tzinfo=UTC)
    assert executed_at <= clock.now()
