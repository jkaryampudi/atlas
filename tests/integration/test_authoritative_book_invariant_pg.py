"""P0.1 (ADR-0018) correction 2: a fail-closed invariant protecting authoritative
whole-book reporting. Builds a REAL open satellite lot via the lifecycle for a
paper strategy (the invariant holds), then downgrades that strategy to
research_shadow — now an open lot is attributable to a non-authoritative
strategy, so assert_authoritative_book and compute_attribution RAISE rather than
silently reporting shadow capital as authoritative book performance."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.portfolio.attribution import (
    NonAuthoritativeBookError,
    assert_authoritative_book,
    compute_attribution,
)
from atlas.dcp.risk.seed_limits import seed_limit_set
from atlas.dcp.trading.proposals import approve, build_proposal, settle_orders
from tests.conftest import requires_pg

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
FX = Decimal("1.5")


def _open_satellite_lot(s, clock) -> None:
    """A held xsmom-pit-tr satellite lot, entered via the real lifecycle
    (build -> approve -> settle fills at the 2026-07-14 open)."""
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency) VALUES "
        "('ZINV','XTEST','US','stock','ZINV','Information Technology','USD') "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid,:d,100,101,99,100,1000000,'EodhdAdapter')"),
        [{"iid": iid, "d": date(2026, 6, 23) + timedelta(days=i)}
         for i in range(21)])
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-10',:r,'inv'),('USD','AUD','2026-07-14',:r,"
        "'inv') ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": FX})
    sid = s.execute(text(
        "INSERT INTO quant.strategies (family,name,version,spec,code_sha,"
        " tolerance_bands,state) VALUES ('xsmom-pit-tr','x','1.0.0','{}','s','{}',"
        " 'paper') RETURNING id")).scalar()
    signal_id = s.execute(text(
        "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
        " direction, rank, formation_return, valid_until, created_at) "
        "VALUES (:sid,:iid,'2026-07-13','long',1,0.5,'2026-08-31',:ca) "
        "RETURNING id"), {"sid": sid, "iid": iid, "ca": clock.now()}).scalar()
    memo_id = str(s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee','ZINV','BUY','[]',:ca) RETURNING id"),
        {"ca": clock.now()}).scalar())
    res = build_proposal(s, clock, memo_id=memo_id, symbol="ZINV",
                         signal_refs=[str(signal_id)], entry_price=Decimal("100"),
                         stop_price=Decimal("95"), target_price=Decimal("120"))
    assert res.state == "pending_approval"
    clock.advance_to(T0 + timedelta(hours=1))
    assert approve(s, clock, proposal_id=res.proposal_id,
                   acknowledged_risks=True).status == "approved"
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid,'2026-07-14',102,104,101,103,1000000,'EodhdAdapter')"),
        {"iid": iid})
    clock.advance_to(datetime(2026, 7, 14, 22, 0, tzinfo=UTC))
    assert len(settle_orders(s, clock)) == 1        # the open lot now exists


def test_invariant_holds_for_a_paper_strategy(clean_audit):
    s = clean_audit
    _open_satellite_lot(s, FrozenClock(T0))
    assert_authoritative_book(s)                     # no raise: strategy is paper
    compute_attribution(s, year=2026, month=7)       # no raise


def test_invariant_fails_closed_when_the_lot_becomes_non_authoritative(clean_audit):
    s = clean_audit
    _open_satellite_lot(s, FrozenClock(T0))
    # the strategy is downgraded AFTER deploying the lot: its open lot is now
    # attributable to a non-authoritative strategy
    s.execute(text("UPDATE quant.strategies SET state='research_shadow' "
                   "WHERE family='xsmom-pit-tr'"))
    with pytest.raises(NonAuthoritativeBookError, match="non-authoritative"):
        assert_authoritative_book(s)
    # the whole-book report refuses rather than mixing shadow capital in
    with pytest.raises(NonAuthoritativeBookError):
        compute_attribution(s, year=2026, month=7)
