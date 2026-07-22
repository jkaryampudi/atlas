"""F-012: the deployed sleeve now has a monthly rebalance-SELL, matching the
validated construct (xsmom_pit_run: `weights = dict(pending)` fully exits every
name that drops out of the winner set at each month-end).

scan_rebalance_exits, on a rebalance session, emits a pre-authorized full-quantity
SELL for every held momentum-sleeve name absent from the current winner set. It is
a no-op on non-rebalance days (=> monthly turnover), under research_shadow, and
for non-momentum holdings; a name already selling (stop/prior rebalance) is not
double-sold."""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.trading.exits import scan_rebalance_exits
from tests.conftest import requires_pg

pytestmark = requires_pg

SIG = date(2026, 7, 31)                 # the current monthly rebalance session
PRIOR = date(2026, 6, 30)              # the rebalance the held names were bought at
CLOCK = FrozenClock(datetime(2026, 8, 4, 20, 0, tzinfo=UTC))   # after SIG's close


def _inst(s, symbol) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency) VALUES (:s,'XTEST','US','stock',:s,'USD') RETURNING id"),
        {"s": symbol}).scalar())


def _bar(s, iid, d=SIG) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high,"
        " low, close, volume, source) VALUES (:i,:d,100,101,99,100,1000,'EodhdAdapter') "
        "ON CONFLICT (instrument_id, bar_date) DO NOTHING"), {"i": iid, "d": d})


def _strategy(s, state="paper") -> str:
    return str(s.execute(text(
        "INSERT INTO quant.strategies (family,name,version,spec,code_sha,"
        " tolerance_bands,state) VALUES ('xsmom-pit-tr','x','1.0.0','{}','sha','{}',"
        " :st) RETURNING id"), {"st": state}).scalar())


def _signal(s, strat, iid, *, on, rank) -> str:
    return str(s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:st,:i,:d,'long',:r,0.5,:vu,:ca) RETURNING id"),
        {"st": strat, "i": iid, "d": on, "r": rank, "vu": SIG,
         "ca": CLOCK.now()}).scalar())


def _memo(s) -> str:
    return str(s.execute(text(
        "INSERT INTO research.memos (memo_type, recommendation, evidence_refs) "
        "VALUES ('committee','BUY','[]') RETURNING id")).scalar())


def _momentum_position(s, iid, *, entry_sig: str, memo: str, qty=10,
                       is_core=False, signal=True) -> str:
    """A held position with a FULL entry lineage (proposal -> check -> approval ->
    filled buy order -> execution -> open tax lot -> position). Attributed to the
    momentum sleeve iff `signal` (its entry proposal cites `entry_sig`)."""
    # a momentum holding cites its entry signal (origin='agent'); a non-momentum
    # holding is a core-allocation buy with no signal lineage (origin permits it)
    sids = f"ARRAY['{entry_sig}']::uuid[]" if signal else "ARRAY[]::uuid[]"
    origin = "agent" if signal else "core_allocation"
    pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " committee_memo_id, signal_ids, entry_price, stop_loss, target_price, "
        " position_size, risks, state, origin, expires_at, created_at) "
        f"VALUES (:i,'US','buy',:m,{sids},100,90,120,:q,'[]','approved',:o,"
        " :e,:t) RETURNING id"),
        {"i": iid, "m": memo, "q": qty, "o": origin, "e": CLOCK.now(),
         "t": CLOCK.now()}).scalar()
    cid = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, price_snapshot, results, verdict,"
        " check_kind) VALUES (:p,'{}','[]','PASS','proposal') RETURNING id"),
        {"p": pid}).scalar()
    aid = s.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, "
        " approval_time_risk_check_id, decided_at) "
        "VALUES (:p,'approve','principal',:c,:t) RETURNING id"),
        {"p": pid, "c": cid, "t": CLOCK.now()}).scalar()
    oid = s.execute(text(
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, broker,"
        " side, qty, order_type, state, created_at) "
        "VALUES (:p,:a,:c,'paper','buy',:q,'market','filled',:t) RETURNING id"),
        {"p": pid, "a": aid, "c": cid, "q": qty, "t": CLOCK.now()}).scalar()
    eid = s.execute(text(
        "INSERT INTO trading.executions (order_id, fill_qty, fill_price, "
        " executed_at, created_at) VALUES (:o,:q,100,:t,:t) RETURNING id"),
        {"o": oid, "q": qty, "t": CLOCK.now()}).scalar()
    posid = s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        " opened_at, current_stop, thesis_memo_id, is_core) "
        "VALUES (:i,:q,100,'USD',:t,90,:m,:core) RETURNING id"),
        {"i": iid, "q": qty, "t": CLOCK.now(), "m": memo, "core": is_core}).scalar()
    s.execute(text(
        "INSERT INTO trading.tax_lots (position_id, execution_id, qty, cost_aud, "
        " acquired_at, created_at) VALUES (:p,:e,:q,1000,:t,:t)"),
        {"p": posid, "e": eid, "q": qty, "t": CLOCK.now()})
    return str(posid)


def _sells(s):
    return s.execute(text(
        "SELECT i.symbol, o.qty FROM trading.orders o "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE o.side='sell' AND o.order_type='rebalance' "
        "  AND o.state='pending_submit'")).all()


def _scenario(s, *, state="paper", held_signal=True, held_is_core=False):
    """A DROP (held, entry sig at PRIOR, absent from SIG winners) + a KEEP (held,
    still a SIG winner). Returns (drop_pos, keep_pos)."""
    strat = _strategy(s, state)
    memo = _memo(s)
    drop = _inst(s, "DROP")
    keep = _inst(s, "KEEP")
    for w in ("WIN2", "WIN3", "WIN4", "WIN5"):
        wid = _inst(s, w)
        _bar(s, wid)
        _signal(s, strat, wid, on=SIG, rank=int(w[-1]))
    _bar(s, drop)
    _bar(s, keep)
    # entry signals at the PRIOR rebalance (what the held names were bought on)
    entry_drop = _signal(s, strat, drop, on=PRIOR, rank=1)
    entry_keep = _signal(s, strat, keep, on=PRIOR, rank=1)
    # KEEP is still a winner this rebalance (rank 1); DROP is not in the SIG set
    _signal(s, strat, keep, on=SIG, rank=1)
    drop_pos = _momentum_position(s, drop, entry_sig=entry_drop, memo=memo,
                                  signal=held_signal, is_core=held_is_core)
    keep_pos = _momentum_position(s, keep, entry_sig=entry_keep, memo=memo)
    return drop_pos, keep_pos


# --------------------------------------------------------------------------- #
def test_dropped_name_is_sold_retained_is_not(clean_audit):
    s = clean_audit
    drop_pos, keep_pos = _scenario(s)
    fired = scan_rebalance_exits(s, CLOCK)
    sold = {r.symbol for r in fired}
    assert sold == {"DROP"}                        # only the dropped name
    assert {r.symbol for r in _sells(s)} == {"DROP"}
    # the sell is the FULL position quantity (validated construct fully exits)
    assert [r.qty for r in _sells(s)] == [10]


def test_non_rebalance_session_is_a_noop(clean_audit):
    """No signals dated at this session => not a rebalance => monthly cadence."""
    s = clean_audit
    strat = _strategy(s)
    memo = _memo(s)
    drop = _inst(s, "DROP")
    _bar(s, drop)
    entry = _signal(s, strat, drop, on=PRIOR, rank=1)   # only a PRIOR-dated signal
    _momentum_position(s, drop, entry_sig=entry, memo=memo)
    assert scan_rebalance_exits(s, CLOCK) == ()          # nothing formed at SIG


def test_research_shadow_is_a_noop(clean_audit):
    s = clean_audit
    _scenario(s, state="research_shadow")
    assert scan_rebalance_exits(s, CLOCK) == ()          # non-authoritative -> no sells


def test_non_momentum_holding_is_not_sold(clean_audit):
    """A holding NOT attributed to the momentum sleeve (no momentum signal in its
    entry lineage) is never touched, even though it is absent from the winners."""
    s = clean_audit
    _scenario(s, held_signal=False)                      # DROP's entry cites no signal
    assert scan_rebalance_exits(s, CLOCK) == ()


def test_in_flight_sell_is_not_double_sold(clean_audit):
    s = clean_audit
    drop_pos, _ = _scenario(s)
    first = scan_rebalance_exits(s, CLOCK)
    assert len(first) == 1
    # a live sell now exists -> a second scan must not create another
    assert scan_rebalance_exits(s, CLOCK) == ()
    assert len(_sells(s)) == 1


def _drop_meta(s, drop_pos):
    return s.execute(text(
        "SELECT instrument_id, thesis_memo_id FROM trading.positions WHERE id=:p"),
        {"p": drop_pos}).one()


def _add_nonmomentum_lot(s, pos_id, iid, memo, qty=5):
    """A second, NON-momentum (core-allocation) open lot on the SAME position —
    ADR-0014's one-row-per-instrument can merge two same-name agent buys."""
    pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " committee_memo_id, signal_ids, entry_price, target_price, position_size, "
        " risks, state, origin, expires_at, created_at) "
        "VALUES (:i,'US','buy',:m,ARRAY[]::uuid[],100,120,:q,'[]','approved',"
        " 'core_allocation',:e,:t) RETURNING id"),
        {"i": iid, "m": memo, "q": qty, "e": CLOCK.now(), "t": CLOCK.now()}).scalar()
    cid = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, price_snapshot, results, verdict,"
        " check_kind) VALUES (:p,'{}','[]','PASS','proposal') RETURNING id"),
        {"p": pid}).scalar()
    aid = s.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, "
        " approval_time_risk_check_id, decided_at) "
        "VALUES (:p,'approve','core',:c,:t) RETURNING id"),
        {"p": pid, "c": cid, "t": CLOCK.now()}).scalar()
    oid = s.execute(text(
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, broker,"
        " side, qty, order_type, state, created_at) "
        "VALUES (:p,:a,:c,'paper','buy',:q,'market','filled',:t) RETURNING id"),
        {"p": pid, "a": aid, "c": cid, "q": qty, "t": CLOCK.now()}).scalar()
    eid = s.execute(text(
        "INSERT INTO trading.executions (order_id, fill_qty, fill_price, executed_at,"
        " created_at) VALUES (:o,:q,100,:t,:t) RETURNING id"),
        {"o": oid, "q": qty, "t": CLOCK.now()}).scalar()
    s.execute(text(
        "INSERT INTO trading.tax_lots (position_id, execution_id, qty, cost_aud, "
        " acquired_at, created_at) VALUES (:p,:e,:q,500,:t,:t)"),
        {"p": pos_id, "e": eid, "q": qty, "t": CLOCK.now()})


def test_pending_exit_proposal_blocks_rebalance_sell(clean_audit):
    """Adversarial-review fix (HIGH): a name with a human EXIT proposal awaiting
    approval (no order yet) must NOT also get a deferred rebalance sell — the two
    sells would wedge settle_orders and roll back every nightly cycle."""
    s = clean_audit
    drop_pos, _ = _scenario(s)
    meta = _drop_meta(s, drop_pos)
    pid = s.execute(text(
        "INSERT INTO trading.trade_proposals (instrument_id, market, action, "
        " committee_memo_id, signal_ids, entry_price, target_price, position_size, "
        " risks, state, origin, expires_at, created_at) "
        "VALUES (:i,'US','exit',:m,ARRAY[]::uuid[],100,100,10,'[]','risk_review',"
        " 'core_allocation',:e,:t) RETURNING id"),
        {"i": meta.instrument_id, "m": meta.thesis_memo_id, "e": CLOCK.now(),
         "t": CLOCK.now()}).scalar()
    cid = s.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, price_snapshot, results, verdict,"
        " check_kind) VALUES (:p,'{}','[]','PASS','proposal') RETURNING id"),
        {"p": pid}).scalar()
    s.execute(text("UPDATE trading.trade_proposals SET state='pending_approval', "
                   "risk_check_id=:c WHERE id=:p"), {"c": cid, "p": pid})
    assert scan_rebalance_exits(s, CLOCK) == ()          # blocked by the exit proposal
    assert _sells(s) == []


def test_comingled_position_is_not_over_sold(clean_audit):
    """Adversarial-review fix: a position co-mingling a momentum lot and a
    non-momentum lot is left alone — a full-qty FIFO sell would over-liquidate the
    non-sleeve shares."""
    s = clean_audit
    drop_pos, _ = _scenario(s)
    meta = _drop_meta(s, drop_pos)
    _add_nonmomentum_lot(s, drop_pos, meta.instrument_id, meta.thesis_memo_id)
    assert scan_rebalance_exits(s, CLOCK) == ()          # co-mingled -> not sold


def test_sell_order_is_pre_authorized_and_reuses_entry_approval(clean_audit):
    s = clean_audit
    drop_pos, _ = _scenario(s)
    scan_rebalance_exits(s, CLOCK)
    row = s.execute(text(
        "SELECT o.approval_id, o.risk_check_id, rc.verdict, "
        "       rc.results->0->>'rule' AS rule "
        "FROM trading.orders o JOIN risk.risk_checks rc ON rc.id = o.risk_check_id "
        "WHERE o.order_type='rebalance'")).one()
    assert row.approval_id is not None and row.risk_check_id is not None
    assert row.verdict == "PASS" and row.rule == "REBALANCE"
