"""Phase-5 exit engine (Doc 04 §5 exit-only breakers, §14 shortfall; Doc 05 §5).

Exercises both ways out of a position against atlas_test: the pre-authorized
stop exit (scan_stop_exits: first-breach trigger over every unscanned bar per
board memo 2026-07 item 6, min(stop, open) gap fill, order_time check,
same-transaction fill, catch_up flagging) and the discretionary close
(close_position -> approve -> next-open settle), plus sell settlement itself —
FIFO lot disposal with a partial-lot split, cash ledger to the cent, breaker
independence (DD3 never blocks an exit), idempotency, and fail-closed skips.

Nothing is committed: the pg_session fixture rolls back, so atlas_test keeps
none of the trading rows created here. Defensive deletes at seed time guard
against debris from previously crashed runs.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.exits import close_position, scan_stop_exits
from atlas.dcp.trading.proposals import (
    approve,
    build_proposal,
    cancel_order,
    reject,
    settle_orders,
    snapshot,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
# Monday 2026-07-13: the first day limit set v1 is effective (seeds/limit_set_v1.json)
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
NEXT_SESSION = date(2026, 7, 14)   # XNYS session after 2026-07-13 (entry fill day)
STOP_SESSION = date(2026, 7, 15)   # XNYS session after the entry fill day
US_OPEN_NEXT = datetime(2026, 7, 14, 13, 30, tzinfo=UTC)
US_OPEN_STOP = datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
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


def _bar(s, iid, d: date, *, open_, low, close) -> None:
    """One OHLC bar (the stop scan needs open AND low, unlike entry history)."""
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, low, close, volume, source) "
        "VALUES (:iid, :d, :o, :l, :c, 1000000, 'EodhdAdapter')"),
        {"iid": iid, "d": d, "o": open_, "l": low, "c": close})


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


def _ztla_id(s):
    return s.execute(text("SELECT id FROM market.instruments WHERE symbol = 'ZTLA'")).scalar()


def _entered_position(s, clock, memo_id: str) -> tuple[str, str]:
    """build -> approve -> settle: ZTLA 53 @ 102.102 (stop 95) filled at the
    2026-07-14 open, exactly as the lifecycle happy path pins it. Leaves the
    clock at 2026-07-14 22:00 UTC. Returns (entry_proposal_id, position_id)."""
    res = _build(s, clock, memo_id)
    assert res.state == "pending_approval"
    clock.advance_to(T0 + timedelta(hours=1))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, close, "
        "volume, source) VALUES (:iid, :d, 102, 103, 1000000, 'EodhdAdapter')"),
        {"iid": _ztla_id(s), "d": NEXT_SESSION})
    _fx(s, day=NEXT_SESSION)
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    assert len(settle_orders(s, clock)) == 1
    position_id = s.execute(text(
        "SELECT id FROM trading.positions WHERE closed_at IS NULL")).scalar()
    return res.proposal_id, str(position_id)


# ------------------------------------------------------------ stop exits (§5)

def test_stop_exit_full_round_trip(clean_audit):
    """buy -> fill -> price falls through the stop -> auto-exit at the stop
    with sell-side costs -> position closed, lot disposed, cash to the cent."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    entry_proposal_id, position_id = _entered_position(s, clock, memo_id)

    # intraday hit, no gap: open 96 > stop 95 >= low 94 -> fill AT the stop
    _bar(s, _ztla_id(s), STOP_SESSION, open_=96, low=94, close=Decimal("94.5"))
    _fx(s, day=STOP_SESSION)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))
    reports = scan_stop_exits(s, clock)
    assert len(reports) == 1
    rep = reports[0]
    assert rep.symbol == "ZTLA"
    assert rep.qty == 53
    assert rep.fill_date == STOP_SESSION
    assert rep.fill_price == Decimal("94.905000")       # 95 * (1 - 10bps)
    assert rep.shortfall_bps == Decimal("10.0000")      # vs the authorized stop

    # pre-authorization lineage: the order references the ORIGINAL entry
    # approval and a FRESH PASS order_time check (no new proposal, no click)
    entry_approval = s.execute(text(
        "SELECT id FROM trading.approvals WHERE decision = 'approve'")).scalar()
    order = s.execute(text(
        "SELECT proposal_id, approval_id, risk_check_id, side, qty, order_type, "
        "state FROM trading.orders WHERE id = :o"), {"o": rep.order_id}).one()
    assert (order.side, order.qty, order.order_type, order.state) == \
        ("sell", 53, "stop", "filled")
    assert str(order.proposal_id) == entry_proposal_id
    assert order.approval_id == entry_approval
    check = s.execute(text(
        "SELECT verdict, check_kind, limit_set_version, results, price_snapshot "
        "FROM risk.risk_checks WHERE id = :c"), {"c": order.risk_check_id}).one()
    assert (check.verdict, check.check_kind) == ("PASS", "order_time")
    assert check.limit_set_version is None              # no limit set consulted
    assert check.results[0]["rule"] == "STOP"
    assert "95.000000 hit by low 94.000000" in check.results[0]["detail"]
    assert check.price_snapshot["fill_price"] == "94.905000"
    assert check.price_snapshot["breaker"] == "none"

    ex = s.execute(text(
        "SELECT fill_qty, fill_price, fees, fx_rate_used, decision_price, "
        "shortfall_bps, executed_at FROM trading.executions WHERE order_id = :o"),
        {"o": rep.order_id}).one()
    assert (ex.fill_qty, ex.fees) == (53, Decimal(0))
    assert ex.fill_price == Decimal("94.905000")
    assert ex.fx_rate_used == FX_USD_AUD                # the bar date's OWN rate
    assert ex.decision_price == Decimal("95.000000")    # Doc 04 §14
    assert ex.shortfall_bps == Decimal("10.0000")
    assert ex.executed_at == US_OPEN_STOP               # the bar day's open

    pos = s.execute(text(
        "SELECT qty, avg_cost, closed_at FROM trading.positions WHERE id = :p"),
        {"p": position_id}).one()
    assert pos.qty == 0
    assert pos.avg_cost == Decimal("102.102000")        # unchanged by the sell
    assert pos.closed_at == US_OPEN_STOP
    lot = s.execute(text(
        "SELECT qty, cost_aud, proceeds_aud, disposed_at FROM trading.tax_lots")).one()
    assert (lot.qty, lot.cost_aud) == (53, Decimal("8117.11"))
    assert lot.proceeds_aud == Decimal("7544.95")       # 53 * 94.905 * 1.5
    assert lot.disposed_at == US_OPEN_STOP

    # cash ledger exact to the cent: 100000 - 8117.109 + 7544.9475
    clock.advance_to(datetime(2026, 7, 15, 23, 0, tzinfo=UTC))
    snap = snapshot(s, clock)
    assert snap.cash_aud == Decimal("99427.84")
    assert snap.nav_aud == Decimal("99427.84")          # nothing held anymore
    assert snap.open_risk_pct == Decimal("0.0000")

    evs = _events(s)
    assert evs.count("position.stop_hit") == 1
    assert evs.count("position.closed") == 1
    assert evs.count("position.reduced") == 0
    assert evs.count("execution.recorded") == 2         # entry + stop exit
    assert evs.count("order.state_changed") == 4
    assert evs.count("risk.check.completed") == 3       # proposal/approval/order_time
    assert evs.count("proposal.executed") == 1          # entry only — never twice

    # a same-run latest-bar fire is NOT a catch-up: no flag, no window size
    hit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'position.stop_hit'")).one()
    assert "catch_up" not in hit.payload
    assert "bars_scanned" not in hit.payload

    # idempotent: the position is closed and the exit order exists
    assert scan_stop_exits(s, clock) == ()
    assert s.execute(text("SELECT count(*) FROM trading.executions")).scalar() == 2


def test_gap_down_fills_at_open(clean_audit):
    """Open 90 gaps THROUGH the stop 95: you cannot fill above where the
    market opened — fill = open, shortfall vs the stop shows the gap cost."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _entered_position(s, clock, memo_id)
    _bar(s, _ztla_id(s), STOP_SESSION, open_=90, low=88, close=89)
    _fx(s, day=STOP_SESSION)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))

    reports = scan_stop_exits(s, clock)
    assert len(reports) == 1
    assert reports[0].fill_price == Decimal("89.910000")     # 90 * (1 - 10bps)
    assert reports[0].shortfall_bps == Decimal("535.7895")   # (95-89.91)/95 bps
    ex = s.execute(text(
        "SELECT decision_price, shortfall_bps FROM trading.executions "
        "WHERE order_id = :o"), {"o": reports[0].order_id}).one()
    assert ex.decision_price == Decimal("95.000000")
    assert ex.shortfall_bps == Decimal("535.7895")
    lot = s.execute(text(
        "SELECT proceeds_aud FROM trading.tax_lots WHERE disposed_at IS NOT NULL")).one()
    assert lot.proceeds_aud == Decimal("7147.84")            # 53 * 89.91 * 1.5


def test_stop_not_hit_nothing_happens(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _, position_id = _entered_position(s, clock, memo_id)
    _bar(s, _ztla_id(s), STOP_SESSION, open_=97, low=96, close=Decimal("96.5"))
    _fx(s, day=STOP_SESSION)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))

    assert scan_stop_exits(s, clock) == ()
    assert s.execute(text("SELECT count(*) FROM trading.orders")).scalar() == 1
    state = s.execute(text("SELECT closed_at FROM trading.positions WHERE id = :p"),
                      {"p": position_id}).scalar()
    assert state is None
    assert "position.stop_hit" not in _events(s)


def test_missing_fill_date_fx_skips_fail_closed(clean_audit):
    """No fill-date FX -> no order, no fill, nothing half-done: the position
    is skipped THIS run and the next scan retries once the rate lands."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _entered_position(s, clock, memo_id)
    _bar(s, _ztla_id(s), STOP_SESSION, open_=96, low=94, close=Decimal("94.5"))
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))

    assert scan_stop_exits(s, clock) == ()               # FX job lags: skip
    assert s.execute(text("SELECT count(*) FROM trading.orders")).scalar() == 1
    assert s.execute(text("SELECT count(*) FROM trading.executions")).scalar() == 1

    _fx(s, day=STOP_SESSION)                             # the rate lands -> retry
    reports = scan_stop_exits(s, clock)
    assert len(reports) == 1
    fx_used = s.execute(text(
        "SELECT fx_rate_used FROM trading.executions WHERE order_id = :o"),
        {"o": reports[0].order_id}).scalar()
    assert fx_used == FX_USD_AUD


def test_dd3_breaker_allows_stop_exit(clean_audit):
    """Doc 04 §5: DD3 is exit-ONLY, never exit-blocking — a latched full halt
    must not stop the protective exit from running."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _entered_position(s, clock, memo_id)
    # latch DD3: NAV history 100000 -> 80000 is a -20% drawdown
    s.execute(text(
        "INSERT INTO trading.portfolio_snapshots (as_of, nav_aud, cash_aud) VALUES "
        "(:t1, 100000, 91882.89), (:t2, 80000, 91882.89)"),
        {"t1": datetime(2026, 7, 14, 22, 30, tzinfo=UTC),
         "t2": datetime(2026, 7, 14, 23, 0, tzinfo=UTC)})
    _bar(s, _ztla_id(s), STOP_SESSION, open_=96, low=94, close=Decimal("94.5"))
    _fx(s, day=STOP_SESSION)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))

    reports = scan_stop_exits(s, clock)
    assert len(reports) == 1                             # ran UNDER DD3
    check = s.execute(text(
        "SELECT price_snapshot FROM risk.risk_checks WHERE check_kind = 'order_time'")).one()
    assert check.price_snapshot["breaker"] == "DD3"      # recorded, not obeyed
    closed = s.execute(text(
        "SELECT closed_at FROM trading.positions")).scalar()
    assert closed == US_OPEN_STOP


def test_missed_cycle_catch_up_fires_on_breach_day(clean_audit):
    """Board memo 2026-07 item 6, the exact defect scenario: the 07-15 and
    07-16 cycles never ran (machine asleep), 07-15's low breached the stop
    and 07-16 healed. The old latest-bar scan saw only a healed bar and
    silently skipped the stop forever; the 07-17 scan must instead fire on
    07-15 at min(stop, its open), stamped with 07-15's session open and
    07-15's OWN FX, and flag the fire as a catch-up ops event."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _, position_id = _entered_position(s, clock, memo_id)

    _bar(s, _ztla_id(s), STOP_SESSION, open_=96, low=94, close=Decimal("95.5"))
    _fx(s, day=STOP_SESSION, rate=Decimal("1.6"))        # D's OWN rate, distinct
    _bar(s, _ztla_id(s), date(2026, 7, 16), open_=97, low=96, close=97)  # healed
    _fx(s, day=date(2026, 7, 16))
    _bar(s, _ztla_id(s), date(2026, 7, 17), open_=98, low=97, close=98)
    _fx(s, day=date(2026, 7, 17))
    clock.advance_to(datetime(2026, 7, 17, 22, 0, tzinfo=UTC))  # 2 missed cycles

    reports = scan_stop_exits(s, clock)
    assert len(reports) == 1
    rep = reports[0]
    assert rep.fill_date == STOP_SESSION                 # the breach day, not today
    assert rep.fill_price == Decimal("94.905000")        # min(95, 96) * (1 - 10bps)
    assert rep.shortfall_bps == Decimal("10.0000")
    ex = s.execute(text(
        "SELECT fx_rate_used, executed_at FROM trading.executions "
        "WHERE order_id = :o"), {"o": rep.order_id}).one()
    assert ex.fx_rate_used == Decimal("1.6")             # 07-15's rate, not 07-17's
    assert ex.executed_at == US_OPEN_STOP                # 07-15's session open
    lot = s.execute(text(
        "SELECT proceeds_aud FROM trading.tax_lots WHERE disposed_at IS NOT NULL")).one()
    assert lot.proceeds_aud == Decimal("8047.94")        # 53 * 94.905 * 1.6
    closed = s.execute(text(
        "SELECT closed_at FROM trading.positions WHERE id = :p"),
        {"p": position_id}).scalar()
    assert closed == US_OPEN_STOP

    hit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'position.stop_hit'")).one()
    assert hit.payload["bar_date"] == "2026-07-15"
    assert hit.payload["catch_up"] is True               # fired late: ops event
    assert hit.payload["bars_scanned"] == 3              # 07-15/16/17 window

    # idempotency survives a catch-up fire: the position is closed, re-scans
    # find nothing, and no second execution ever appears
    assert scan_stop_exits(s, clock) == ()
    assert s.execute(text("SELECT count(*) FROM trading.executions")).scalar() == 2


def test_multi_day_gap_fires_first_breach_not_deepest(clean_audit):
    """Two breach bars inside the missed window: the fill belongs to the
    FIRST breach day (07-15, intraday touch at the stop) — a live broker's
    stop was already done that day — never the deeper 07-16 gap."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _entered_position(s, clock, memo_id)

    _bar(s, _ztla_id(s), STOP_SESSION, open_=96, low=Decimal("94.5"),
         close=Decimal("94.5"))                          # first breach: at the stop
    _fx(s, day=STOP_SESSION)
    _bar(s, _ztla_id(s), date(2026, 7, 16), open_=90, low=88, close=89)  # deeper
    _fx(s, day=date(2026, 7, 16))
    _bar(s, _ztla_id(s), date(2026, 7, 17), open_=91, low=90, close=91)
    _fx(s, day=date(2026, 7, 17))
    clock.advance_to(datetime(2026, 7, 17, 22, 0, tzinfo=UTC))

    reports = scan_stop_exits(s, clock)
    assert len(reports) == 1
    assert reports[0].fill_date == STOP_SESSION          # 07-15, not 07-16
    assert reports[0].fill_price == Decimal("94.905000")  # NOT 89.910000
    ex = s.execute(text(
        "SELECT executed_at FROM trading.executions WHERE order_id = :o"),
        {"o": reports[0].order_id}).one()
    assert ex.executed_at == US_OPEN_STOP
    hit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'position.stop_hit'")).one()
    assert hit.payload["bar_date"] == "2026-07-15"
    assert hit.payload["catch_up"] is True
    assert hit.payload["bars_scanned"] == 3
    assert scan_stop_exits(s, clock) == ()               # fires exactly once


def test_missing_breach_day_fx_fails_closed_with_reason(clean_audit):
    """A catch-up fill needs the BREACH date's own FX. 07-15 breached but its
    rate never landed; 07-16 healed and has one. The scan must not borrow a
    neighbouring day's rate: it skips the position with an AUDITED reason
    (a breached-but-unfillable stop is an ops condition, not a silent skip)
    and completes the catch-up once 07-15's rate lands."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _entered_position(s, clock, memo_id)

    _bar(s, _ztla_id(s), STOP_SESSION, open_=96, low=94, close=Decimal("94.5"))
    _bar(s, _ztla_id(s), date(2026, 7, 16), open_=97, low=96, close=97)  # healed
    _fx(s, day=date(2026, 7, 16))                        # D+1 has FX; D does NOT
    clock.advance_to(datetime(2026, 7, 16, 22, 0, tzinfo=UTC))

    assert scan_stop_exits(s, clock) == ()               # fail closed
    assert s.execute(text("SELECT count(*) FROM trading.orders")).scalar() == 1
    assert s.execute(text("SELECT count(*) FROM trading.executions")).scalar() == 1
    skip = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'position.stop_scan_skipped'")).one()
    assert skip.payload["reason"] == "fx_missing_for_breach_date"
    assert skip.payload["bar_date"] == "2026-07-15"      # the breach day's rate
    assert skip.payload["symbol"] == "ZTLA"

    _fx(s, day=STOP_SESSION, rate=Decimal("1.6"))        # the rate lands -> retry
    reports = scan_stop_exits(s, clock)
    assert len(reports) == 1
    assert reports[0].fill_date == STOP_SESSION
    fx_used = s.execute(text(
        "SELECT fx_rate_used FROM trading.executions WHERE order_id = :o"),
        {"o": reports[0].order_id}).scalar()
    assert fx_used == Decimal("1.6")                     # 07-15's own rate
    hit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'position.stop_hit'")).one()
    assert hit.payload["catch_up"] is True
    assert hit.payload["bars_scanned"] == 2              # 07-15 + 07-16


def test_addon_raised_stop_does_not_retrofire_on_older_bars(clean_audit):
    """The scan floor is the LATEST lot acquisition (module docstring):
    _record_fill's tighten-only merge can RAISE the stop at an add-on fill,
    and bars that printed under the OLD lower stop must not be re-judged
    against the raised one — that would fabricate a fill no broker made.
    Bars after the add-on fire against the raised stop as usual. (Add-on is
    hand-applied: L8 blocks add-on buys through the lifecycle.)"""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _, position_id = _entered_position(s, clock, memo_id)  # stop 95, filled 07-14

    # 07-15 low 96 never touched the stop that was live that day (95)
    _bar(s, _ztla_id(s), STOP_SESSION, open_=97, low=96, close=97)
    _fx(s, day=STOP_SESSION)
    # add-on at the 07-16 open raises the stop 95 -> 98 (tighten-only merge)
    s.execute(text(
        "UPDATE trading.positions SET qty = qty + 10, current_stop = 98 "
        "WHERE id = :p"), {"p": position_id})
    s.execute(text(
        "INSERT INTO trading.tax_lots (position_id, qty, cost_aud, acquired_at) "
        "VALUES (:p, 10, 1500.00, :at)"),
        {"p": position_id, "at": datetime(2026, 7, 16, 13, 30, tzinfo=UTC)})
    _bar(s, _ztla_id(s), date(2026, 7, 16), open_=99, low=Decimal("98.5"), close=99)
    _fx(s, day=date(2026, 7, 16))
    clock.advance_to(datetime(2026, 7, 16, 22, 0, tzinfo=UTC))
    # 07-15's low 96 <= raised stop 98, but it predates the raise: NO fire
    assert scan_stop_exits(s, clock) == ()
    assert "position.stop_hit" not in _events(s)

    # a bar AFTER the add-on that touches the raised stop fires normally,
    # for the WHOLE protected quantity
    _bar(s, _ztla_id(s), date(2026, 7, 17), open_=99, low=Decimal("97.5"), close=98)
    _fx(s, day=date(2026, 7, 17))
    clock.advance_to(datetime(2026, 7, 17, 22, 0, tzinfo=UTC))
    reports = scan_stop_exits(s, clock)
    assert len(reports) == 1
    assert reports[0].fill_date == date(2026, 7, 17)
    assert reports[0].qty == 63                          # 53 entry + 10 add-on
    assert reports[0].fill_price == Decimal("97.902000")  # 98 * (1 - 10bps)
    closed = s.execute(text(
        "SELECT closed_at FROM trading.positions WHERE id = :p"),
        {"p": position_id}).scalar()
    assert closed == datetime(2026, 7, 17, 13, 30, tzinfo=UTC)


# ------------------------------------------------------ discretionary close

def test_discretionary_close_full_flow(clean_audit):
    """close_position -> approve -> settle at next open -> position closed.
    The exit proposal reuses the position's thesis memo and the entry
    proposal's signal_ids; L1-L11 do not apply (exits reduce risk)."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    entry_proposal_id, position_id = _entered_position(s, clock, memo_id)

    clock.advance_to(datetime(2026, 7, 14, 23, 0, tzinfo=UTC))
    res = close_position(s, clock, position_id=position_id, reason="thesis broken")
    assert (res.state, res.verdict, res.qty) == ("pending_approval", "PASS", 53)
    row = s.execute(text(
        "SELECT action, committee_memo_id, signal_ids, entry_price, stop_loss, "
        "       target_price, position_size, position_value_aud, state, risk_check_id "
        "FROM trading.trade_proposals WHERE id = :p"), {"p": res.proposal_id}).one()
    assert row.action == "exit"
    assert str(row.committee_memo_id) == memo_id         # thesis memo justifies exit
    entry_signals = s.execute(text(
        "SELECT signal_ids FROM trading.trade_proposals WHERE id = :p"),
        {"p": entry_proposal_id}).scalar()
    assert row.signal_ids == entry_signals               # entry signals, verbatim
    assert row.entry_price == Decimal("103.000000")      # latest close
    assert row.stop_loss == Decimal("95.000000")         # current stop
    assert row.target_price == Decimal("103.000000")
    assert row.position_size == 53
    assert row.position_value_aud == Decimal("8188.50")  # 53 * 103 * 1.5
    assert str(row.risk_check_id) == res.risk_check_id
    check = s.execute(text(
        "SELECT verdict, check_kind, limit_set_version, results "
        "FROM risk.risk_checks WHERE id = :c"), {"c": res.risk_check_id}).one()
    assert (check.verdict, check.check_kind) == ("PASS", "proposal")
    assert check.limit_set_version is None
    assert check.results == [{"rule": "EXIT", "pass": True, "value": None,
                              "limit": None,
                              "detail": "risk-reducing: closes 53 ZTLA"}]

    # approve: fresh approval_time check (EXIT premise + breaker statement),
    # approvals row, sell order pending_submit — parallel to the buy path
    clock.advance_to(datetime(2026, 7, 14, 23, 30, tzinfo=UTC))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"
    fresh = s.execute(text(
        "SELECT verdict, check_kind, results FROM risk.risk_checks WHERE id = :c"),
        {"c": outcome.risk_check_id}).one()
    assert (fresh.verdict, fresh.check_kind) == ("PASS", "approval_time")
    assert [r["rule"] for r in fresh.results] == ["DD", "EXIT"]
    order = s.execute(text(
        "SELECT side, qty, order_type, state FROM trading.orders WHERE id = :o"),
        {"o": outcome.order_id}).one()
    assert (order.side, order.qty, order.order_type, order.state) == \
        ("sell", 53, "market", "pending_submit")

    # settle at the NEXT session's open (2026-07-15), sell-side costs applied
    _bar(s, _ztla_id(s), STOP_SESSION, open_=101, low=100, close=Decimal("101.5"))
    _fx(s, day=STOP_SESSION)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))
    fills = settle_orders(s, clock)
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("100.899000")  # 101 * (1 - 10bps)
    assert fills[0].shortfall_bps == Decimal("203.9806")  # vs decision close 103
    pos = s.execute(text(
        "SELECT qty, closed_at FROM trading.positions WHERE id = :p"),
        {"p": position_id}).one()
    assert (pos.qty, pos.closed_at) == (0, US_OPEN_STOP)
    lot = s.execute(text(
        "SELECT proceeds_aud, disposed_at FROM trading.tax_lots")).one()
    assert lot.proceeds_aud == Decimal("8021.47")        # 53 * 100.899 * 1.5
    assert lot.disposed_at == US_OPEN_STOP
    state = s.execute(text(
        "SELECT state FROM trading.trade_proposals WHERE id = :p"),
        {"p": res.proposal_id}).scalar()
    assert state == "executed"

    clock.advance_to(datetime(2026, 7, 15, 23, 0, tzinfo=UTC))
    snap = snapshot(s, clock)
    assert snap.cash_aud == Decimal("99904.36")   # 100000 - 8117.109 + 8021.4705
    assert snap.nav_aud == Decimal("99904.36")

    evs = _events(s)
    assert evs.count("proposal.created") == 2            # entry + exit
    assert evs.count("proposal.approved") == 2
    assert evs.count("proposal.executed") == 2
    assert evs.count("position.closed") == 1
    assert "position.stop_hit" not in evs


def test_pending_exit_order_blocks_stop_scan(clean_audit):
    """Idempotency: an exit already in flight (any state) blocks the stop
    scan — the same shares must never be sold twice."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _, position_id = _entered_position(s, clock, memo_id)
    clock.advance_to(datetime(2026, 7, 14, 23, 0, tzinfo=UTC))
    res = close_position(s, clock, position_id=position_id, reason="getting out")
    clock.advance_to(datetime(2026, 7, 14, 23, 30, tzinfo=UTC))
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"                  # sell order now pending

    _bar(s, _ztla_id(s), STOP_SESSION, open_=96, low=94, close=Decimal("94.5"))
    _fx(s, day=STOP_SESSION)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))
    assert scan_stop_exits(s, clock) == ()               # exit order exists: skip
    assert s.execute(text("SELECT count(*) FROM trading.orders")).scalar() == 2

    fills = settle_orders(s, clock)                      # the human's exit fills
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("95.904000")   # 96 open * (1 - 10bps)
    assert s.execute(text("SELECT count(*) FROM trading.executions")).scalar() == 2
    closed = s.execute(text(
        "SELECT closed_at FROM trading.positions WHERE id = :p"),
        {"p": position_id}).scalar()
    assert closed == US_OPEN_STOP
    assert scan_stop_exits(s, clock) == ()               # still nothing to do


def test_cancelled_exit_order_rearms_the_stop(clean_audit):
    """Review finding: a CANCELLED sell order is withdrawn intent, not standing
    intent — after cancel_order the position is still open and the protective
    stop must fire again. Anything else silently disarms the stop forever."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _, position_id = _entered_position(s, clock, memo_id)
    clock.advance_to(datetime(2026, 7, 14, 23, 0, tzinfo=UTC))
    res = close_position(s, clock, position_id=position_id, reason="changed my mind")
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "approved"
    assert outcome.order_id is not None
    cancel_order(s, clock, order_id=outcome.order_id, reason="changed it back")

    _bar(s, _ztla_id(s), STOP_SESSION, open_=96, low=94, close=Decimal("94.5"))
    _fx(s, day=STOP_SESSION)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))
    reports = scan_stop_exits(s, clock)                  # the stop is live again
    assert len(reports) == 1
    assert reports[0].fill_price == Decimal("94.905000")  # stop 95 * (1 - 10bps)
    closed = s.execute(text(
        "SELECT closed_at FROM trading.positions WHERE id = :p"),
        {"p": position_id}).scalar()
    assert closed == US_OPEN_STOP
    assert scan_stop_exits(s, clock) == ()               # and only fires once


def test_approve_exit_after_stop_closed_position_voids(clean_audit):
    """The stop wins the race: a pending exit proposal approved AFTER the stop
    already closed the position fails its EXIT re-check and voids — the same
    RISK_RECHECK_FAILED shape as the buy path, never a double sell."""
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _, position_id = _entered_position(s, clock, memo_id)
    clock.advance_to(datetime(2026, 7, 14, 23, 0, tzinfo=UTC))
    res = close_position(s, clock, position_id=position_id, reason="too slow")

    # a pending exit PROPOSAL (no order yet) does NOT block the stop scan:
    # stop protection stays live until the human actually approves
    _bar(s, _ztla_id(s), STOP_SESSION, open_=96, low=94, close=Decimal("94.5"))
    _fx(s, day=STOP_SESSION)
    clock.advance_to(datetime(2026, 7, 15, 22, 0, tzinfo=UTC))
    assert len(scan_stop_exits(s, clock)) == 1

    clock.advance_to(datetime(2026, 7, 15, 22, 30, tzinfo=UTC))  # inside the TTL
    outcome = approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    assert outcome.status == "RISK_RECHECK_FAILED"
    assert outcome.failures == ("EXIT",)
    assert outcome.order_id is None
    state = s.execute(text(
        "SELECT state FROM trading.trade_proposals WHERE id = :p"),
        {"p": res.proposal_id}).scalar()
    assert state == "voided"
    fresh = s.execute(text(
        "SELECT verdict, check_kind FROM risk.risk_checks WHERE id = :c"),
        {"c": outcome.risk_check_id}).one()
    assert (fresh.verdict, fresh.check_kind) == ("FAIL", "approval_time")
    assert s.execute(text("SELECT count(*) FROM trading.orders")).scalar() == 2


def test_reject_exit_proposal_keeps_position(clean_audit):
    s = clean_audit
    memo_id = _seed(s)
    clock = FrozenClock(T0)
    _, position_id = _entered_position(s, clock, memo_id)
    clock.advance_to(datetime(2026, 7, 14, 23, 0, tzinfo=UTC))
    res = close_position(s, clock, position_id=position_id, reason="nerves")

    # only ONE exit may be in flight per instrument
    with pytest.raises(ValueError, match="already in flight"):
        close_position(s, clock, position_id=position_id, reason="again")

    outcome = reject(s, clock, proposal_id=res.proposal_id, reason="hold the line")
    assert outcome.status == "rejected"
    state = s.execute(text(
        "SELECT state FROM trading.trade_proposals WHERE id = :p"),
        {"p": res.proposal_id}).scalar()
    assert state == "rejected"
    appr = s.execute(text(
        "SELECT decision FROM trading.approvals WHERE proposal_id = :p"),
        {"p": res.proposal_id}).scalar()
    assert appr == "reject"
    closed = s.execute(text(
        "SELECT closed_at FROM trading.positions WHERE id = :p"),
        {"p": position_id}).scalar()
    assert closed is None                                # still holding

    # a rejected exit is terminal, not in flight: the human can try again
    res2 = close_position(s, clock, position_id=position_id, reason="sure now")
    assert res2.state == "pending_approval"


# --------------------------------------------------------- sell settlement

def _seed_two_lot_position(s, *, proposal_state: str, check_kind: str,
                           order_qty: int) -> tuple[str, str]:
    """Direct-SQL ZTLB position with TWO tax lots and a pending sell order in
    full lineage (proposal -> check -> approval -> order). Built by hand
    because L8 blocks add-on buys through the lifecycle (correlation with the
    held symbol fails closed) — known and fine. Returns (position_id, order_id)."""
    iid = _instrument(s, "ZTLB", sector="Financials")
    _bar(s, iid, NEXT_SESSION, open_=102, low=101, close=103)
    _fx(s, day=NEXT_SESSION)
    memo = _memo(s)
    position_id = s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        " opened_at, current_stop, thesis_memo_id) "
        "VALUES (:iid, 70, 100, 'USD', :at, 95, :memo) RETURNING id"),
        {"iid": iid, "at": datetime(2026, 7, 10, 13, 30, tzinfo=UTC),
         "memo": memo}).scalar()
    # created_at pinned to acquired_at (not the DB now() default): the FIFO
    # disposal stamps a split residual lot's created_at from the INJECTED clock
    # (~T0), and ORDER BY acquired_at, created_at, id tie-breaks same-acquired_at
    # lots on it. Leaving created_at to now() made the seed real-wall-clock
    # dependent — it inverted once the real date rolled past the frozen T0
    # (CLAUDE.md invariant 6: no wall clock in deterministic paths).
    s.execute(text(
        "INSERT INTO trading.tax_lots (position_id, qty, cost_aud, acquired_at, "
        " created_at) VALUES "
        "(:p, 30, 4500.00, :a1, :a1), (:p, 40, 6200.00, :a2, :a2)"),
        {"p": position_id, "a1": datetime(2026, 7, 10, 13, 30, tzinfo=UTC),
         "a2": datetime(2026, 7, 11, 13, 30, tzinfo=UTC)})
    pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        " position_size, state, expires_at, created_at) "
        "VALUES (:iid, 'US', 'exit', :memo, :sids, 100, 95, 100, :q, :st, :exp, :t) "
        "RETURNING id"),
        {"iid": iid, "memo": memo, "sids": [uuid4()], "q": order_qty,
         "st": proposal_state, "exp": T0 + timedelta(hours=24), "t": T0}).scalar()
    cid = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, results, verdict, check_kind) "
        "VALUES (:p, '[]', 'PASS', :k) RETURNING id"), {"p": pid, "k": check_kind}).scalar()
    aid = s.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, "
        " approval_time_risk_check_id, decided_at) "
        "VALUES (:p, 'approve', 'principal', :c, :t) RETURNING id"),
        {"p": pid, "c": cid, "t": T0}).scalar()
    order_id = s.execute(text(
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, broker, "
        " side, qty, order_type, state, created_at) "
        "VALUES (:p, :a, :c, 'paper', 'sell', :q, 'market', 'pending_submit', :t) "
        "RETURNING id"),
        {"p": pid, "a": aid, "c": cid, "q": order_qty, "t": T0}).scalar()
    return str(position_id), str(order_id)


def test_partial_reduce_disposes_lots_fifo_and_splits(clean_audit):
    """A 45-share sell against lots [30, 40]: lot 1 fully disposed, lot 2
    SPLIT 15/25 — pro-rata cost to the cent, residual keeps its acquisition
    date, avg_cost untouched, position reduced not closed."""
    s = clean_audit
    _seed(s)
    position_id, order_id = _seed_two_lot_position(
        s, proposal_state="approved", check_kind="approval_time", order_qty=45)
    clock = FrozenClock(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))

    fills = settle_orders(s, clock)
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("101.898000")   # 102 open * (1 - 10bps)
    assert fills[0].shortfall_bps == Decimal("-189.8000")  # sold ABOVE decision 100

    lots = s.execute(text(
        "SELECT qty, cost_aud, proceeds_aud, acquired_at, disposed_at "
        "FROM trading.tax_lots ORDER BY acquired_at, created_at, id")).all()
    assert len(lots) == 3
    # lot 1 (oldest): fully disposed at 30 * 101.898 * 1.5
    assert (lots[0].qty, lots[0].cost_aud) == (30, Decimal("4500.00"))
    assert lots[0].proceeds_aud == Decimal("4585.41")
    assert lots[0].disposed_at == US_OPEN_NEXT
    # lot 2 original row = the disposed slice: 15 shares, pro-rata cost
    assert (lots[1].qty, lots[1].cost_aud) == (15, Decimal("2325.00"))
    assert lots[1].proceeds_aud == Decimal("2292.70")     # 15 * 101.898 * 1.5
    assert lots[1].disposed_at == US_OPEN_NEXT
    # residual: 25 shares, EXACT residual cost, same acquisition date, open
    assert (lots[2].qty, lots[2].cost_aud) == (25, Decimal("3875.00"))
    assert lots[2].proceeds_aud is None and lots[2].disposed_at is None
    assert lots[2].acquired_at == lots[1].acquired_at

    pos = s.execute(text(
        "SELECT qty, avg_cost, closed_at, current_stop FROM trading.positions "
        "WHERE id = :p"), {"p": position_id}).one()
    assert (pos.qty, pos.closed_at) == (25, None)         # reduced, not closed
    assert pos.avg_cost == Decimal("100.000000")          # unchanged on reduce
    assert pos.current_stop == Decimal("95.000000")
    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'position.reduced'")).one()
    assert ev.payload["qty"] == 45
    assert ev.payload["remaining_qty"] == 25
    assert ev.payload["proceeds_aud"] == "6878.11"        # 4585.41 + 2292.70
    assert "position.closed" not in _events(s)


def test_stray_order_time_order_settles_without_tripping_verifier(clean_audit):
    """The settle verifier's documented rule: a PASS order_time check on an
    'executed' proposal is genuine stop-exit lineage, not tampering — it must
    fill as a plain sell at the next open, never raise."""
    s = clean_audit
    _seed(s)
    position_id, _ = _seed_two_lot_position(
        s, proposal_state="executed", check_kind="order_time", order_qty=70)
    clock = FrozenClock(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))

    fills = settle_orders(s, clock)                       # must NOT raise
    assert len(fills) == 1
    pos = s.execute(text(
        "SELECT qty, closed_at FROM trading.positions WHERE id = :p"),
        {"p": position_id}).one()
    assert (pos.qty, pos.closed_at) == (0, US_OPEN_NEXT)  # full 70-share exit
    # the already-executed proposal is not re-transitioned or re-announced
    assert "proposal.executed" not in _events(s)
