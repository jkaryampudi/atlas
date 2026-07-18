"""Policy-conformance, DB half (risk-wiring bundle, 2026-07-18).

Builds REAL proposals through the full memo->bridge->build_proposal path on
the isolated test database and asserts the PERSISTED risk-check rows carry
every rule the signed policy claims — DD, L1-L11, STRESS (§7), FACTOR (§12),
VOL (§11) on an evaluated proposal, and SIZING on a §4 rejection — so an
unwiring regression fails loudly. Plus the two bridge guards the bundle added
(earnings_print_imminent, reentry_cooling), the VOL day-step accumulation
semantics, FACTOR catching what L3 structurally cannot, and the named
no-averaging-down pin. Structural (grep-based) call-site checks live in
tests/unit/test_policy_conformance.py. Seeding mirrors test_bridge_pg.py.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.bridge import bridge_memos
from atlas.dcp.trading.proposals import build_proposal
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
# Monday 2026-07-13: the first day limit set v1 is effective (seeds/limit_set_v1.json)
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
FX_USD_AUD = Decimal("1.5")
REFS = ("bars:ZPC:2026-07-13", "gate:momentum_v1:ZPC")

# Every rule id the signed policy claims for an evaluated BUY proposal, in
# the exact persisted order: engine.validate's itemised block, then the
# risk-wiring overlay. THIS LIST IS THE CONFORMANCE PIN — a rule silently
# dropping out of the persisted check is exactly the regression this file
# exists to catch.
EXPECTED_RULES = ["DD", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9",
                  "L10", "L11", "STRESS", "FACTOR", "VOL"]


# ------------------------------------------------------------------- seeding

def _clean(s) -> None:
    """Remove any committed debris from crashed runs (FK-safe order)."""
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM risk.limit_sets WHERE version > 1"))
    s.execute(text(
        "DELETE FROM market.earnings_calendar WHERE instrument_id IN "
        "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZPC%')"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZPC%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZPC%'"))


def _seed(s) -> None:
    _clean(s)
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2026-07-10', :r, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": FX_USD_AUD})


def _instrument(s, symbol: str, *, sector: str = "Information Technology"):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, :sec, 'USD') RETURNING id"),
        {"sym": symbol, "sec": sector}).scalar()


def _ohlc(s, iid, *, days: int = 21, o=100, h=101, lo=99, c=100,
          volume: int = 1_000_000, start: date = date(2026, 6, 23)) -> None:
    """Full OHLC sessions ending 2026-07-13 for the default 21 days."""
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "VALUES (:iid, :d, :o, :h, :l, :c, :v, 'EodhdAdapter')"),
        [{"iid": iid, "d": start + timedelta(days=i), "o": o, "h": h, "l": lo,
          "c": c, "v": volume} for i in range(days)])


def _alternating_bars(s, iid, *, days: int, end: date, phase: int,
                      volume: int = 1_000_000) -> None:
    """`days` consecutive calendar days of bars ending at `end`, with closes
    alternating 100/101 by (day index + phase) parity. Two instruments seeded
    with opposite phase produce PERFECTLY ANTI-correlated daily returns
    (r = -1), which clears both the L8 fail-closed thin-history rule (61+
    overlapping returns) and the 0.8 threshold — so tests can isolate FACTOR
    from L8."""
    rows = []
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        close = 100 if (i + phase) % 2 == 0 else 101
        rows.append({"iid": iid, "d": d, "o": close, "h": close + 1,
                     "l": close - 1, "c": close, "v": volume})
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "VALUES (:iid, :d, :o, :h, :l, :c, :v, 'EodhdAdapter')"), rows)


def _memo(s, clock, symbol: str | None, *, recommendation: str = "BUY",
          refs: tuple[str, ...] = REFS, memo_type: str = "committee",
          created_at: datetime | None = None) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES (:mt, :sym, :rec, CAST(:er AS jsonb), :ca) RETURNING id"),
        {"mt": memo_type, "sym": symbol, "rec": recommendation,
         "er": json.dumps(list(refs)),
         "ca": created_at if created_at is not None else clock.now()}).scalar())


def _check_results(s, proposal_id) -> list[dict]:
    return s.execute(text(
        "SELECT results FROM risk.risk_checks WHERE proposal_id = :p "
        "AND check_kind = 'proposal'"), {"p": proposal_id}).scalar_one()


def _proposal_row(s, symbol: str):
    return s.execute(text(
        "SELECT tp.* FROM trading.trade_proposals tp "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE i.symbol = :sym"), {"sym": symbol}).one()


def _seed_stop_exit(s, iid, *, exit_at: datetime) -> None:
    """A completed stop-exit disposal for `iid`: entry proposal (executed) ->
    approval -> order_type='stop' sell order (filled) -> execution at
    `exit_at`. This is the lineage shape atlas.dcp.trading.exits writes."""
    entry_memo = s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee', 'ZPCA', 'BUY', '[]', :ca) RETURNING id"),
        {"ca": exit_at - timedelta(days=30)}).scalar()   # stale: never a candidate
    pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        "committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        "position_size, state, expires_at, created_at) "
        "VALUES (:iid, 'US', 'buy', :m, :sids, 100, 96, 108, 10, 'executed', "
        ":exp, :ca) RETURNING id"),
        {"iid": iid, "m": entry_memo, "sids": [uuid.uuid4()],
         "exp": exit_at - timedelta(days=29),
         "ca": exit_at - timedelta(days=30)}).scalar()
    cid = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, results, verdict, check_kind) "
        "VALUES (:p, '[]', 'PASS', 'order_time') RETURNING id"), {"p": pid}).scalar()
    aid = s.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, "
        "approval_time_risk_check_id) "
        "VALUES (:p, 'approve', 'principal', :c) RETURNING id"),
        {"p": pid, "c": cid}).scalar()
    oid = s.execute(text(
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, "
        "broker, side, qty, order_type, state, created_at, closed_at) "
        "VALUES (:p, :a, :c, 'paper', 'sell', 10, 'stop', 'filled', :t, :t) "
        "RETURNING id"), {"p": pid, "a": aid, "c": cid, "t": exit_at}).scalar()
    s.execute(text(
        "INSERT INTO trading.executions (order_id, fill_qty, fill_price, fees, "
        "fx_rate_used, broker_exec_id, decision_price, shortfall_bps, "
        "executed_at, created_at) "
        "VALUES (:o, 10, 96, 0, :fx, :bx, 96, 0, :t, :t)"),
        {"o": oid, "fx": FX_USD_AUD, "bx": f"paper-{oid}", "t": exit_at})


# --------------------------------------------- every rule row, one real build

def test_proposal_check_persists_every_policy_rule_row(clean_audit):
    """THE conformance pin: a memo bridged through the full live path persists
    one risk-check row whose itemised results carry EXACTLY the policy's rule
    ids in order — DD gate, L1-L11, STRESS, FACTOR, VOL — with the overlay's
    documented v1 semantics readable in each detail. Empty A$100k book,
    calm ZPCA series (entry 100, ATR stop 96, 53 shares, cost A$7,950)."""
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZPCA"))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZPCA")

    report = bridge_memos(s, clock)
    assert len(report.built) == 1 and report.built[0].verdict == "PASS"
    row = _proposal_row(s, "ZPCA")
    assert row.state == "pending_approval"
    results = _check_results(s, row.id)
    assert [r["rule"] for r in results] == EXPECTED_RULES
    assert all(r["pass"] for r in results)

    by_rule = {r["rule"]: r for r in results}
    # STRESS §7: pro-forma AND without-proposal crash numbers are auditable.
    # 53 shares x $100 x 1.5 = A$7,950; crash US leg -20% -> -0.0159 of NAV.
    assert by_rule["STRESS"]["detail"] == (
        "broad-equity-crash pro-forma -0.0159 (without proposal 0.0000) "
        "vs limit -0.25")
    # FACTOR §12 v1 scope: class-level market beta 1.0 (loading == gross),
    # GICS sector weights, sleeve-membership momentum (0 here: uuid5 refs).
    assert by_rule["FACTOR"]["detail"] == (
        "market 0.0795 vs cap 1.0, momentum 0.0000 vs cap 0.5, "
        "max sector Information Technology 0.0795 vs cap 0.25")
    # VOL §11: post-trade gross and the day's cumulative committed step. The
    # ceiling TRACKS L5 (Principal 2026-07-18): this suite seeds v1 (L5=0.20)
    # so the cap is 1 - 0.20 = 0.80; under v2 (L5=0.10) it would read 0.90.
    assert by_rule["VOL"]["detail"] == (
        "post-trade gross 0.0795 vs max 0.80 (= 1 - L5), "
        "day gross increase 0.0795 vs max step 0.10, breaker none")


def test_sizing_rejection_persists_the_sizing_rule(clean_audit):
    """The §4 arm of the conformance claim: a sizing rejection persists the
    single itemised SIZING row (a proposal with no size has nothing to stress
    or load) and lands terminally 'rejected'."""
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZPCA"))
    clock = FrozenClock(T0)
    memo_id = _memo(s, clock, "ZPCA")

    res = build_proposal(
        s, clock, memo_id=memo_id, symbol="ZPCA",
        signal_refs=[str(uuid.uuid4())], entry_price=Decimal("100"),
        stop_price=Decimal("100"),          # stop not below entry -> §4 reject
        target_price=Decimal("108"))
    assert (res.verdict, res.state, res.failures) == ("FAIL", "rejected",
                                                      ("SIZING",))
    results = _check_results(s, uuid.UUID(res.proposal_id))
    assert [r["rule"] for r in results] == ["SIZING"]
    assert not results[0]["pass"]


# ------------------------------------------------------- VOL §11 day-step cap

def test_vol_step_cap_blocks_same_day_gross_accumulation(clean_audit):
    """Two 7.95%-of-NAV names in ONE day: the first passes (step 0.0795), the
    second fails VOL alone — its own book/L-rules are clean (the first
    proposal holds no order yet, so no L-rule sees it), but the day's
    committed gross reaches 15.9% > MAX_STEP. This is the accumulation hole
    the per-proposal L-rules structurally cannot close, and the FAIL is an
    honest recorded 'rejected' outcome."""
    s = clean_audit
    _seed(s)
    _ohlc(s, _instrument(s, "ZPCA"))
    _ohlc(s, _instrument(s, "ZPCB", sector="Financials"))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZPCA", created_at=T0 - timedelta(minutes=2))
    _memo(s, clock, "ZPCB", created_at=T0 - timedelta(minutes=1))

    report = bridge_memos(s, clock)
    assert [b.verdict for b in report.built] == ["PASS", "FAIL"]
    assert _proposal_row(s, "ZPCB").state == "rejected"
    results = _check_results(s, _proposal_row(s, "ZPCB").id)
    fails = [r["rule"] for r in results if not r["pass"]]
    assert fails == ["VOL"]
    vol = next(r for r in results if r["rule"] == "VOL")
    assert "day gross increase 0.1590 > max step 0.10" in vol["detail"]


# --------------------------------------------- FACTOR §12 vs L3's blind spot

def test_factor_overlap_fails_what_l3_cannot_see(clean_audit):
    """§12's reason to exist: L3 prices only the PROPOSAL's sector, so a book
    already over-cap in an UNRELATED sector waves a new buy through — FACTOR
    itemises every sector and fails it. Existing ZPCF (Financials) marks at
    230 x $100 x 1.5 = A$34,500 = 25.65% of the A$134,500 NAV (> 0.25 cap);
    the candidate is IT, so L3 passes and FACTOR alone fails. Anti-correlated
    70-day series keep L8 out of the verdict (r = -1)."""
    s = clean_audit
    _seed(s)
    ita = _instrument(s, "ZPCA")
    fin = _instrument(s, "ZPCF", sector="Financials")
    _alternating_bars(s, ita, days=70, end=date(2026, 7, 13), phase=0)
    _alternating_bars(s, fin, days=70, end=date(2026, 7, 13), phase=1)
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at, current_stop) VALUES (:iid, 230, 100, 'USD', :t, 99)"),
        {"iid": fin, "t": datetime(2026, 6, 1, 15, 0, tzinfo=UTC)})
    clock = FrozenClock(T0)
    memo_id = _memo(s, clock, "ZPCA")

    res = build_proposal(
        s, clock, memo_id=memo_id, symbol="ZPCA",
        signal_refs=[str(uuid.uuid4())],
        entry_price=Decimal("100"), stop_price=Decimal("96"),
        target_price=Decimal("108"))
    assert (res.verdict, res.state) == ("FAIL", "rejected")
    assert res.failures == ("FACTOR",)          # L3 passed; FACTOR did not
    results = _check_results(s, uuid.UUID(res.proposal_id))
    by_rule = {r["rule"]: r for r in results}
    assert by_rule["L3"]["pass"] and by_rule["L8"]["pass"]
    assert "BREACH: sector Financials" in by_rule["FACTOR"]["detail"]


# ------------------------------------------- earnings-print guard (bridge)

def test_earnings_print_imminent_is_a_recorded_skip(clean_audit):
    """A known report on the next XNYS session (2026-07-14) blocks the memo
    with the report date in the recorded reason; no proposal exists."""
    s = clean_audit
    _seed(s)
    iid = _instrument(s, "ZPCA")
    _ohlc(s, iid)
    s.execute(text(
        "INSERT INTO market.earnings_calendar (instrument_id, report_date, "
        "when_time, fetched_at, source) "
        "VALUES (:iid, '2026-07-14', 'BeforeMarket', :t, 'test')"),
        {"iid": iid, "t": T0})
    clock = FrozenClock(T0)
    _memo(s, clock, "ZPCA")

    report = bridge_memos(s, clock)
    assert report.built == ()
    assert len(report.skipped) == 1
    reason = report.skipped[0].reason
    assert reason.startswith("earnings_print_imminent")
    assert "2026-07-14" in reason
    assert s.execute(text(
        "SELECT count(*) FROM trading.trade_proposals")).scalar() == 0


def test_earnings_beyond_guard_window_or_past_never_block(clean_audit):
    """Only STRICTLY FUTURE reports inside the 2-XNYS-session window block:
    a report dated the decision day already happened (or prints tonight,
    before the fill) and a report on the third session out (2026-07-16;
    horizon is 07-15) is outside the guard. Absence of calendar data is
    likewise no block — every other test in this file bridges with an empty
    calendar."""
    s = clean_audit
    _seed(s)
    iid = _instrument(s, "ZPCA")
    _ohlc(s, iid)
    s.execute(text(
        "INSERT INTO market.earnings_calendar (instrument_id, report_date, "
        "fetched_at, source) VALUES (:iid, '2026-07-13', :t, 'test'), "
        "(:iid, '2026-07-16', :t, 'test')"), {"iid": iid, "t": T0})
    clock = FrozenClock(T0)
    _memo(s, clock, "ZPCA")

    report = bridge_memos(s, clock)
    assert report.skipped == ()
    assert len(report.built) == 1 and report.built[0].verdict == "PASS"


# --------------------------------------------- re-entry cooling (bridge)

def test_reentry_cooling_blocks_memo_created_before_the_stop_out(clean_audit):
    """ZPCA stopped out at today's open (2026-07-13 14:30Z); the memo was
    written YESTERDAY — its thesis predates the exit, so re-entry inside the
    10-session window is the recorded skip 'reentry_cooling'."""
    s = clean_audit
    _seed(s)
    iid = _instrument(s, "ZPCA")
    _ohlc(s, iid)
    _seed_stop_exit(s, iid, exit_at=datetime(2026, 7, 13, 14, 30, tzinfo=UTC))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZPCA", created_at=T0 - timedelta(days=1))

    report = bridge_memos(s, clock)
    assert report.built == ()
    assert len(report.skipped) == 1
    reason = report.skipped[0].reason
    assert reason.startswith("reentry_cooling")
    assert "2026-07-13" in reason and "0 of 10" in reason


def test_memo_created_after_the_stop_out_is_the_policy_exception(clean_audit):
    """The signed policy's own exception, not a loophole: a committee memo
    created AFTER the stop-out IS the 'new committee memo' and bridges even
    though fewer than 10 sessions have elapsed."""
    s = clean_audit
    _seed(s)
    iid = _instrument(s, "ZPCA")
    _ohlc(s, iid)
    _seed_stop_exit(s, iid, exit_at=datetime(2026, 7, 13, 14, 30, tzinfo=UTC))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZPCA", created_at=datetime(2026, 7, 13, 18, 0, tzinfo=UTC))

    report = bridge_memos(s, clock)
    assert report.skipped == ()
    assert len(report.built) == 1 and report.built[0].verdict == "PASS"


def test_reentry_after_cooling_period_is_served_passes(clean_audit):
    """A stop-out on 2026-06-23 is 13 XNYS sessions back (July 3 is the
    Independence Day observance) — outside the 10-session window: the cooling
    clock has run, and any fresh memo bridges."""
    s = clean_audit
    _seed(s)
    iid = _instrument(s, "ZPCA")
    _ohlc(s, iid)
    _seed_stop_exit(s, iid, exit_at=datetime(2026, 6, 23, 14, 30, tzinfo=UTC))
    clock = FrozenClock(T0)
    _memo(s, clock, "ZPCA", created_at=T0 - timedelta(days=1))

    report = bridge_memos(s, clock)
    assert report.skipped == ()
    assert len(report.built) == 1 and report.built[0].verdict == "PASS"


# ------------------------------------------------- no averaging down (Doc 03)

def test_no_averaging_down_open_position_skip_is_the_policy_call_site(clean_audit):
    """Doc 03 prohibited activities — 'no averaging down past the original
    risk budget'. This pin names the policy: the bridge's open-position skip
    IS its call site (bridge.py module docstring). A fresh BUY memo for a
    held, drawn-down name is ALWAYS a recorded skip — the agent lane cannot
    add to a position at ANY price, so sizing past the original risk budget
    is unrepresentable through it. (tests/unit/test_policy_conformance.py
    pins that the bridge is build_proposal's only live caller.)"""
    s = clean_audit
    _seed(s)
    iid = _instrument(s, "ZPCA")
    _ohlc(s, iid)
    s.execute(text(   # held at 100, marked at 100 — the memo 'likes the dip'
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at, current_stop) VALUES (:iid, 53, 110, 'USD', :t, 95)"),
        {"iid": iid, "t": datetime(2026, 7, 10, 15, 0, tzinfo=UTC)})
    clock = FrozenClock(T0)
    _memo(s, clock, "ZPCA")

    report = bridge_memos(s, clock)
    assert report.built == ()
    assert len(report.skipped) == 1
    assert "open position" in report.skipped[0].reason
    assert s.execute(text(
        "SELECT count(*) FROM trading.trade_proposals")).scalar() == 0
