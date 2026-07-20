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
    NON_AUTHORITATIVE_CLOSED_LOT,
    NON_AUTHORITATIVE_STRATEGY,
    UNKNOWN_STRATEGY_STATE,
    PortfolioIntegrityError,
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


def _downgrade(s, state="research_shadow") -> None:
    s.execute(text("UPDATE quant.strategies SET state=:st "
                   "WHERE family='xsmom-pit-tr'"), {"st": state})


def _dispose_the_lot(s) -> None:
    """Close the open satellite lot (simulate a full sell) so no OPEN lot remains
    — the disposed lot + its fill still contribute to realised P&L / shortfall."""
    s.execute(text("UPDATE trading.tax_lots SET disposed_at = now(), "
                   "proceeds_aud = 8000 WHERE disposed_at IS NULL "
                   "AND execution_id IS NOT NULL"))


def test_open_research_shadow_lot_fails_authoritative_reporting(clean_audit):
    """Test 1: an OPEN research-shadow lot fails authoritative reporting."""
    s = clean_audit
    _open_satellite_lot(s, FrozenClock(T0))
    _downgrade(s)
    with pytest.raises(PortfolioIntegrityError) as ei:
        assert_authoritative_book(s)
    assert ei.value.reason_code == NON_AUTHORITATIVE_STRATEGY


def test_disposed_research_shadow_lot_fails_realised_pnl(clean_audit):
    """Tests 2 + 14: a CLOSED/disposed research-shadow lot fails realised-P&L
    reporting (a PostgreSQL closed-corrupted-lot case)."""
    s = clean_audit
    _open_satellite_lot(s, FrozenClock(T0))
    _dispose_the_lot(s)
    _downgrade(s)
    with pytest.raises(PortfolioIntegrityError) as ei:
        compute_attribution(s, year=2026, month=7)      # realised P&L path
    assert ei.value.reason_code == NON_AUTHORITATIVE_CLOSED_LOT


def test_research_shadow_fill_fails_even_with_no_open_lot(clean_audit):
    """Test 3: with the lot fully disposed (no open lot remains), the fill/closed
    lot still resolves to the shadow strategy and fails closed."""
    s = clean_audit
    _open_satellite_lot(s, FrozenClock(T0))
    _dispose_the_lot(s)
    _downgrade(s)
    assert s.execute(text("SELECT count(*) FROM trading.tax_lots "
                          "WHERE disposed_at IS NULL")).scalar() == 0
    with pytest.raises(PortfolioIntegrityError):
        assert_authoritative_book(s)


def test_a_lot_with_no_resolvable_strategy_is_authoritative(clean_audit):
    """Tests 4/5 boundary + 10: a non-core lot that resolves to NO strategy
    (a manual/discretionary holding — the only 'no-lineage' record a real book
    can hold, since FK guarantees a real satellite lot's signal resolves to a
    strategy) is authoritative by default. It makes no non-authoritative claim,
    so the guard passes — a valid book with a manual holding is unaffected."""
    s = clean_audit
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) VALUES "
        "('ZORPH','XTEST','US','stock','ZORPH','USD') RETURNING id")).scalar()
    pos = s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        " opened_at, is_core, created_at) VALUES (:iid,10,10,'USD',now(),false,"
        " now()) RETURNING id"), {"iid": iid}).scalar()
    s.execute(text(
        "INSERT INTO trading.tax_lots (position_id, execution_id, qty, cost_aud, "
        " acquired_at, created_at) VALUES (:p, NULL, 10, 150, now(), now())"),
        {"p": pos})
    assert_authoritative_book(s)                    # no raise: no shadow claim


def test_non_authoritative_lifecycle_state_fails_closed(clean_audit):
    """Test 6: a lot resolving to a strategy in a non-authoritative, non-shadow
    lifecycle state (e.g. suspended) is refused — never valid for authoritative
    reporting."""
    s = clean_audit
    _open_satellite_lot(s, FrozenClock(T0))
    _downgrade(s, state="suspended")
    with pytest.raises(PortfolioIntegrityError) as ei:
        assert_authoritative_book(s)
    assert ei.value.reason_code == UNKNOWN_STRATEGY_STATE


def test_a_disposed_shadow_lot_cannot_disappear_from_the_check(clean_audit):
    """Test 7: an inner-join-style guard that looked only at OPEN lots would
    silently drop a fully-disposed shadow lot (it 'disappears'); the expanded
    guard covers disposed lots, so the record cannot vanish unnoticed."""
    s = clean_audit
    _open_satellite_lot(s, FrozenClock(T0))
    _dispose_the_lot(s)
    _downgrade(s)
    # an OPEN-only query returns nothing — the record would 'disappear'
    open_only = s.execute(text(
        "SELECT count(*) FROM trading.tax_lots tl "
        "JOIN trading.executions e ON e.id = tl.execution_id "
        "JOIN trading.orders o ON o.id = e.order_id "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "JOIN quant.signals sig ON sig.id = ANY(tp.signal_ids) "
        "JOIN quant.strategies st ON st.id = sig.strategy_id "
        "WHERE tl.disposed_at IS NULL AND NOT (st.state IN ('paper','live'))")
    ).scalar()
    assert open_only == 0                           # open-only guard sees nothing
    with pytest.raises(PortfolioIntegrityError):    # the expanded guard catches it
        assert_authoritative_book(s)


def test_integrity_error_carries_structured_reason_and_logs(clean_audit, caplog):
    """Test 13: the exception carries a structured reason_code + safe identifiers,
    and a structured log line is emitted."""
    import logging
    s = clean_audit
    _open_satellite_lot(s, FrozenClock(T0))
    _downgrade(s)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(PortfolioIntegrityError) as ei:
            assert_authoritative_book(s)
    assert ei.value.reason_code == NON_AUTHORITATIVE_STRATEGY
    assert "lot_id" in ei.value.identifiers and "state" in ei.value.identifiers
    assert any("integrity breach" in r.message and "reason=" in r.message
               for r in caplog.records)
