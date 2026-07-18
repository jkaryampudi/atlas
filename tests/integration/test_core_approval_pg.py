"""Core-proposal approval + limit_set_v2 against an isolated Postgres.

Wires the two gaps that block the passive index core (ADR-0012/0014, option B)
from deploying end to end:

  * approve() no longer chokes on a core proposal's NULL stop. A core BUY
    (origin='core_allocation') is rebalanced, not stopped — approve() runs the
    fresh §2.2 re-check with the leg represented as stopless (stop == entry =>
    zero L6/L7 risk) while the weight rules still bind, so SPY at 55% clears
    L2's 0.60 core cap under v2 and lands 'approved' with core lineage that
    settlement turns into an is_core position.
  * the SAFETY the fix must NOT break: an AGENT proposal with a NULL stop still
    fails closed (the stopless treatment is gated on origin ALONE, never on a
    missing stop). The DB structurally forbids that row; we drop the guard
    transiently (rolled back with the txn) to prove approve() refuses anyway.
  * atlas.tools.seed_limit_set_v2 inserts v2 derived from v1 with ONLY the
    ADR-0014 deltas, supersedes v1, single confirmation_a, and refuses a
    duplicate.

Run ONLY against a dedicated throwaway DB, never dev 'atlas' or shared
'atlas_test':

    export ATLAS_TEST_DATABASE_URL="postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas_test_coreappr"

Nothing commits: clean_audit rolls back.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.engine import load_active_limit_set
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.core_allocation import build_core_proposals
from atlas.dcp.trading.proposals import approve, settle_orders
from atlas.tools.seed_limit_set_v2 import seed_limit_set_v2
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)   # first day limit_set v1 is effective
NEXT_SESSION = date(2026, 7, 14)                 # XNYS session after 2026-07-13 (Mon)
SPY_PX = Decimal("751.83")
FX_USD_AUD = Decimal("1.4453")
_HIST = [date(2026, 6, 23) + timedelta(days=i) for i in range(21)]   # >=20 sessions
APPROVER = "Jay Karyampudi (Principal)"
DECISION = "ADR-0014"


# ------------------------------------------------------------------- seeding

def _clean(s) -> None:
    s.execute(text("UPDATE trading.trade_proposals SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol IN ('SPY','INDA'))"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol IN ('SPY','INDA')"))


def _etf(s, symbol: str, exposure: str) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, sector_gics, currency, economic_exposure) "
        "VALUES (:sym, :sym, 'US', 'etf', :sym, 'Broad', 'USD', ARRAY[:exp]) "
        "RETURNING id"), {"sym": symbol, "exp": exposure}).scalar())


def _bars(s, iid: str, close: Decimal, days=_HIST) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, 10000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": close} for d in days])


def _fx(s, days) -> None:
    for d in days:
        s.execute(text(
            "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
            "VALUES ('USD', 'AUD', :d, :r, 'test') "
            "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
            {"d": d, "r": FX_USD_AUD})


def _seed_v1_and_spy(s) -> str:
    """Empty A$100k book + SPY at its golden close + a USD->AUD rate + limit v1."""
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = _etf(s, "SPY", "US")
    _bars(s, iid, SPY_PX)
    _fx(s, [date(2026, 7, 10)])
    return iid


# ------------------------------------------------- Gap 2: the seed tool first

def test_seed_limit_set_v2_inserts_correct_limits_and_supersedes_v1(clean_audit):
    s = clean_audit
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")

    new_id = seed_limit_set_v2(s, FrozenClock(T0), approved_by=APPROVER,
                               decision_ref=DECISION)

    row = s.execute(text(
        "SELECT id, version, mode, limits, effective_from, created_by, "
        "       confirmation_a, confirmation_b, supersedes "
        "FROM risk.limit_sets WHERE version = 2")).one()
    assert str(row.id) == new_id
    assert row.mode == "small_aum"
    lim = row.limits
    # ONLY the ADR-0014 deltas changed; everything else inherited from v1.
    assert lim["L5_min_cash_reserve"] == 0.10                 # 0.20 -> 0.10
    assert lim["L2_core_index_etf_weight"] == 0.60            # new core cap
    assert lim["core_index_etf_allowlist"] == ["SPY", "INDA"]  # new allowlist
    assert lim["L2_max_etf_weight"] == 0.15                   # other ETFs unchanged
    assert lim["L7_max_aggregate_open_risk"] == 0.06          # L7 value unchanged
    assert lim["L9_max_new_positions_per_day"] == 2           # L9 value unchanged
    assert lim["L1_max_stock_weight"] == 0.08                 # inherited verbatim
    # governance: supersedes v1's VERSION (the int column cannot hold v1's uuid),
    # single confirmation, injected-clock effective date, approver+ref attribution.
    assert row.supersedes == 1
    assert row.confirmation_a is not None and row.confirmation_b is None
    assert row.effective_from == date(2026, 7, 13)
    assert APPROVER in row.created_by and DECISION in row.created_by

    # the engine loader picks up v2 (highest version effective today) and parses
    # the new keys into the typed Limits.
    limits = load_active_limit_set(s, date(2026, 7, 13))
    assert limits.version == 2
    assert limits.l2_core_index_etf_weight == Decimal("0.60")
    assert limits.core_index_etf_allowlist == frozenset({"SPY", "INDA"})
    assert limits.l2_cap_for("SPY") == Decimal("0.60")
    assert limits.l2_cap_for("QQQ") == Decimal("0.15")

    # a material governance action emits an audit event (invariant 4).
    assert s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'risk.limit_set.created'")).scalar() == 1


def test_seed_limit_set_v2_refuses_duplicate(clean_audit):
    s = clean_audit
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    seed_limit_set_v2(s, FrozenClock(T0), approved_by=APPROVER, decision_ref=DECISION)
    with pytest.raises(RuntimeError, match="already has version 2"):
        seed_limit_set_v2(s, FrozenClock(T0), approved_by=APPROVER,
                          decision_ref=DECISION)


# --------------------------------- Gap 1: approve() on a stopless core proposal

def test_approve_core_proposal_passes_and_settles_is_core(clean_audit):
    """The deliverable: a 55% SPY core leg builds -> approve() re-check PASSES
    under v2 (L2 core cap 0.60) despite its NULL stop -> the approved order
    carries the core lineage -> settlement opens an is_core, stopless position."""
    s = clean_audit
    iid = _seed_v1_and_spy(s)
    seed_limit_set_v2(s, FrozenClock(T0), approved_by=APPROVER, decision_ref=DECISION)
    clock = FrozenClock(T0)

    # build the SPY core leg (55%). Under v2 it clears L2 and lands pending_approval.
    built = build_core_proposals(s, clock, targets={"SPY": Decimal("0.55")})
    assert len(built) == 1
    leg = built[0]
    assert (leg.symbol, leg.action, leg.qty) == ("SPY", "buy", 50)
    assert leg.verdict == "PASS" and leg.state == "pending_approval"

    # approve() runs the fresh re-check on the stopless core proposal -> PASS.
    outcome = approve(s, clock, proposal_id=leg.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"
    assert outcome.order_id is not None and outcome.risk_check_id is not None

    # the fresh approval-time check is a genuine PASS and its L2 admitted 55%.
    chk = s.execute(text(
        "SELECT verdict, check_kind, results FROM risk.risk_checks WHERE id = :c"),
        {"c": outcome.risk_check_id}).one()
    assert chk.verdict == "PASS" and chk.check_kind == "approval_time"
    l2 = next(r for r in chk.results if r["rule"] == "L2")
    assert l2["pass"] and float(l2["limit"]) == 0.60 and abs(float(l2["value"]) - 0.5433) < 1e-3

    # proposal is approved with core lineage; the order references the PASS check.
    prop = s.execute(text(
        "SELECT state, origin, stop_loss FROM trading.trade_proposals WHERE id = :p"),
        {"p": leg.proposal_id}).one()
    assert prop.state == "approved" and prop.origin == "core_allocation"
    assert prop.stop_loss is None                 # core is stopless, and approved
    order = s.execute(text(
        "SELECT side, state, risk_check_id FROM trading.orders WHERE id = :o"),
        {"o": outcome.order_id}).one()
    assert order.side == "buy" and order.state == "pending_submit"
    assert str(order.risk_check_id) == outcome.risk_check_id

    # settle: seed the next XNYS session's open bar + FX, then fill through the
    # real settle_orders -> _record_fill path. The position is is_core, stopless.
    _bars(s, iid, SPY_PX, days=[NEXT_SESSION])
    _fx(s, [NEXT_SESSION])
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    fills = settle_orders(s, clock)
    assert len(fills) == 1
    pos = s.execute(text(
        "SELECT is_core, current_stop, qty FROM trading.positions "
        "WHERE instrument_id = :i AND closed_at IS NULL"), {"i": iid}).one()
    assert pos.is_core is True and pos.current_stop is None and int(pos.qty) == 50


def test_build_book_survives_a_pending_core_order_null_stop(clean_audit):
    """REGRESSION (adversarial review 2026-07-18, pre-existing at HEAD): once a
    core leg is APPROVED it becomes a pending_submit order carrying a NULL stop.
    _build_book selects every pending buy with no origin filter, so it must
    represent that order as is_core=True / risk None — mirroring the holdings
    rule — never Decimal(NULL). Before the fix, the hardcoded is_core=False +
    Decimal(o.stop_loss) raised the instant a core order sat unfilled, taking
    down every subsequent build_proposal / approve / core-maintenance call."""
    from atlas.dcp.trading.proposals import _build_book

    s = clean_audit
    _seed_v1_and_spy(s)
    seed_limit_set_v2(s, FrozenClock(T0), approved_by=APPROVER, decision_ref=DECISION)
    clock = FrozenClock(T0)

    leg = build_core_proposals(s, clock, targets={"SPY": Decimal("0.55")})[0]
    outcome = approve(s, clock, proposal_id=leg.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"           # order now in pending_submit
    order = s.execute(text(
        "SELECT state FROM trading.orders WHERE id = :o"),
        {"o": outcome.order_id}).one()
    assert order.state == "pending_submit"        # the exact crash precondition

    # the call that used to raise Decimal(None): build the worst-case book.
    book = _build_book(s, clock)
    spy = next(h for h in book.state.holdings if h.symbol == "SPY")
    assert spy.is_core is True                     # origin-derived, not hardcoded
    assert spy.risk_to_stop_aud is None            # core carries no stop-out risk


def test_approve_core_proposal_blocked_under_v1_l2(clean_audit):
    """Control: with ONLY v1 active (no core cap), the SAME 55% SPY leg is
    TERMINAL-rejected by L2 at build time — it never reaches approve(). This is
    the current-state blocker v2 lifts; invariant 3 intact."""
    s = clean_audit
    _seed_v1_and_spy(s)   # no v2
    clock = FrozenClock(T0)
    built = build_core_proposals(s, clock, targets={"SPY": Decimal("0.55")})
    assert len(built) == 1 and built[0].verdict == "FAIL"
    assert built[0].state == "rejected" and "L2" in built[0].failures


def test_approve_agent_proposal_with_null_stop_fails_closed(clean_audit):
    """SAFETY PIN: approve()'s stopless treatment is gated on origin ALONE. An
    AGENT proposal with a NULL stop must STILL fail — never read as zero risk.

    The DB structurally forbids that row (migration 0022's
    trade_proposals_agent_requires_stop). We drop the guard transiently — the
    whole test rolls back — purely to construct the impossible state and prove
    approve() refuses it anyway (fails closed with a loud error), rather than
    silently applying the core stop==entry shortcut."""
    s = clean_audit
    iid = _seed_v1_and_spy(s)
    clock = FrozenClock(T0)

    # transiently drop the invariant guard (restored by the txn rollback).
    s.execute(text("ALTER TABLE trading.trade_proposals "
                   "DROP CONSTRAINT trade_proposals_agent_requires_stop"))
    memo = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, recommendation, evidence_refs) "
        "VALUES ('committee', 'BUY', '[]') RETURNING id")).scalar())
    # a proposal-time PASS check is required to sit in 'pending_approval'.
    rc = s.execute(text(
        "INSERT INTO risk.risk_checks (results, verdict, check_kind, price_snapshot) "
        "VALUES ('[]', 'PASS', 'proposal', CAST('{\"breaker\":\"none\"}' AS jsonb)) "
        "RETURNING id")).scalar()
    pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, origin, "
        " committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        " position_size, state, risk_check_id, expires_at, created_at) "
        "VALUES (:i, 'US', 'buy', 'agent', :m, :sig, 100, NULL, 120, 10, "
        "        'pending_approval', :rc, :x, :c) RETURNING id"),
        {"i": iid, "m": memo, "sig": [uuid4()], "rc": rc,
         "x": T0 + timedelta(hours=24), "c": T0}).scalar()

    with pytest.raises(RuntimeError, match="NULL"):
        approve(s, clock, proposal_id=str(pid), acknowledged_risks=True)
    # the agent proposal did NOT get approved — it stays pending_approval.
    assert s.execute(text(
        "SELECT state FROM trading.trade_proposals WHERE id = :p"),
        {"p": pid}).scalar() == "pending_approval"
