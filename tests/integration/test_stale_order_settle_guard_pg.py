"""F-026: a BUY approved while its strategy was authoritative must NOT fill if
the strategy was downgraded to research_shadow before settle — the stale
approval cannot deploy capital. It is voided fail-closed."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.proposals import approve, build_proposal, settle_orders
from tests.conftest import requires_pg

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
FX = Decimal("1.5")


def _approved_buy_from_a_paper_strategy(s, clock):
    """Build+approve a real buy from a PAPER xsmom strategy (ZSTALE); return the
    strategy id so the test can downgrade it before settle."""
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, sector_gics, currency) VALUES ('ZSTALE','XTEST','US','stock',"
        "'ZSTALE','Information Technology','USD') RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high,"
        " low, close, volume, source) VALUES (:iid,:d,100,101,99,100,1000000,"
        "'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)} for i in range(21)])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-10',:r,'x'),('USD','AUD','2026-07-14',:r,'x') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"), {"r": FX})
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family,name,version,spec,code_sha,"
        " tolerance_bands,state) VALUES ('xsmom-pit-tr','x','1.0.0','{}','s','{}',"
        " 'paper') RETURNING id")).scalar()
    sig = s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid,:iid,'2026-07-13','long',1,0.5,'2026-08-31',:ca) RETURNING id"),
        {"sid": sid, "iid": iid, "ca": clock.now()}).scalar()
    memo = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation,"
        " evidence_refs, created_at) VALUES ('committee','ZSTALE','BUY','[]',:ca) "
        "RETURNING id"), {"ca": clock.now()}).scalar())
    res = build_proposal(s, clock, memo_id=memo, symbol="ZSTALE",
                         signal_refs=[str(sig)], entry_price=Decimal("100"),
                         stop_price=Decimal("95"), target_price=Decimal("120"))
    clock.advance_to(T0 + timedelta(hours=1))
    approve(s, clock, proposal_id=res.proposal_id, acknowledged_risks=True)
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high,"
        " low, close, volume, source) VALUES (:iid,'2026-07-14',102,104,101,103,"
        "1000000,'EodhdAdapter')"), {"iid": iid})
    return str(sid), res.proposal_id


def test_downgraded_lineage_buy_is_voided_not_filled(clean_audit):
    s = clean_audit
    clock = FrozenClock(T0)
    sid, pid = _approved_buy_from_a_paper_strategy(s, clock)
    # the strategy is downgraded to research_shadow AFTER the buy was approved
    s.execute(text("UPDATE quant.strategies SET state='research_shadow' WHERE id=:i"),
              {"i": sid})
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    fills = settle_orders(s, clock)
    assert fills == ()                                   # nothing deployed
    # the order is cancelled and the proposal voided
    ostate = s.execute(text(
        "SELECT state FROM trading.orders WHERE proposal_id=:p"), {"p": pid}).scalar()
    pstate = s.execute(text(
        "SELECT state FROM trading.trade_proposals WHERE id=:p"), {"p": pid}).scalar()
    assert ostate == "cancelled" and pstate == "voided"
    # no capital moved: no executions / tax lots
    assert s.execute(text("SELECT count(*) FROM trading.executions")).scalar() == 0


def test_authoritative_lineage_buy_still_fills(clean_audit):
    """Control: while the strategy remains paper, the buy fills normally."""
    s = clean_audit
    clock = FrozenClock(T0)
    _sid, pid = _approved_buy_from_a_paper_strategy(s, clock)
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    fills = settle_orders(s, clock)
    assert len(fills) == 1
    assert s.execute(text(
        "SELECT state FROM trading.orders WHERE proposal_id=:p"), {"p": pid}).scalar() == "filled"
