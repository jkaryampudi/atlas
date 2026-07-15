"""Pure rebalancer math for the passive index core (ADR-0012).

plan_core_rebalance is deterministic and side-effect-free: NAV + target weights
+ current book -> integer-share buy/sell legs, whole shares only, never
over-allocating, acting only outside the +/-5pp drift band. These pins are the
golden numbers the persistence path (and the orchestrator) rely on.
"""
from decimal import Decimal

import pytest

from atlas.dcp.trading.core_allocation import (
    CORE_TARGETS,
    plan_core_rebalance,
)

# Golden fixture: A$100k book, signed default weights, empty holdings.
NAV = Decimal("100000")
SPY_PX = Decimal("751.83")
INDA_PX = Decimal("48.73")
FX = Decimal("1.4453")   # AUD per 1 USD
PRICES = {"SPY": SPY_PX, "INDA": INDA_PX}
FXMAP = {"SPY": FX, "INDA": FX}
_CENT = Decimal("0.01")


def _plan(positions, **kw):
    return plan_core_rebalance(
        nav_aud=NAV, targets=CORE_TARGETS, positions=positions,
        prices=PRICES, fx=FXMAP, **kw)


def test_golden_empty_book_hand_computed_shares_and_residual():
    """Empty book -> two buys sized to the whole-share holding just under target.

    SPY: target 55% of A$100k = A$55,000; price A$1,086.619899/sh
         -> floor(55000 / 1086.619899) = 50 shares (A$54,330.99, residual A$669.01).
    INDA: target 15% = A$15,000; price A$70.429469/sh
         -> floor(15000 / 70.429469) = 212 shares (A$14,931.05, residual A$68.95).
    """
    legs = {leg.symbol: leg for leg in _plan({})}
    assert set(legs) == {"SPY", "INDA"}

    spy = legs["SPY"]
    assert (spy.action, spy.qty, spy.resulting_qty) == ("buy", 50, 50)
    assert spy.ref_price * spy.fx_to_aud == Decimal("1086.619899")
    assert spy.target_value_aud == Decimal("55000.00")
    assert spy.resulting_value_aud.quantize(_CENT) == Decimal("54330.99")
    assert spy.cash_residual_aud.quantize(_CENT) == Decimal("669.01")

    inda = legs["INDA"]
    assert (inda.action, inda.qty, inda.resulting_qty) == ("buy", 212, 212)
    assert inda.ref_price * inda.fx_to_aud == Decimal("70.429469")
    assert inda.target_value_aud == Decimal("15000.00")
    assert inda.resulting_value_aud.quantize(_CENT) == Decimal("14931.05")
    assert inda.cash_residual_aud.quantize(_CENT) == Decimal("68.95")

    # never over-allocate: each leg lands at or below its target, and the book
    # deploys ~69.26% leaving a documented A$30,737.96 cash residual.
    assert spy.resulting_value_aud <= spy.target_value_aud
    assert inda.resulting_value_aud <= inda.target_value_aud
    deployed = spy.resulting_value_aud + inda.resulting_value_aud
    assert (NAV - deployed).quantize(_CENT) == Decimal("30737.96")


def test_rebalance_is_idempotent_at_the_resulting_holdings():
    """Re-running against the book the first pass produces -> zero legs."""
    first = _plan({})
    holdings = {leg.symbol: leg.resulting_qty for leg in first}
    assert _plan(holdings) == []


def test_within_drift_band_yields_no_legs():
    """Both legs inside +/-5pp of target -> hold, no proposals (idempotent).

    SPY 47 sh = 51.07% (3.9pp from 55%); INDA 184 sh = 12.96% (2.0pp from 15%).
    """
    legs = _plan({"SPY": 47, "INDA": 184})
    assert legs == []


def test_outside_band_buys_the_underweight_and_sells_the_overweight():
    """SPY underweight (39.1%, 15.9pp low) -> BUY; INDA overweight (24.5%,
    9.5pp high) -> SELL. Both breach the +/-5pp band."""
    legs = {leg.symbol: leg for leg in _plan({"SPY": 36, "INDA": 348})}

    spy = legs["SPY"]
    assert spy.action == "buy"
    assert spy.resulting_qty == 50           # floor(55000 / 1086.619899)
    assert spy.qty == 50 - 36                 # 14 to buy

    inda = legs["INDA"]
    assert inda.action == "sell"
    assert inda.resulting_qty == 212          # floor(15000 / 70.429469)
    assert inda.qty == 348 - 212              # 136 to sell
    # a sell still never over-allocates: it trims to <= target
    assert inda.resulting_value_aud <= inda.target_value_aud


def test_custom_drift_band_widens_the_hold_zone():
    """A 20pp band holds a 39.1% SPY that a 5pp band would rebalance."""
    assert _plan({"SPY": 36, "INDA": 212}, drift_band_pp=Decimal("20")) == []
    assert any(leg.symbol == "SPY" for leg in
               _plan({"SPY": 36, "INDA": 212}, drift_band_pp=Decimal("5")))


def test_rejects_non_positive_inputs():
    with pytest.raises(ValueError, match="nav_aud must be positive"):
        plan_core_rebalance(nav_aud=Decimal(0), targets=CORE_TARGETS,
                            positions={}, prices=PRICES, fx=FXMAP)
    with pytest.raises(ValueError, match="price and fx must be positive"):
        plan_core_rebalance(nav_aud=NAV, targets={"SPY": Decimal("0.55")},
                            positions={}, prices={"SPY": Decimal(0)},
                            fx={"SPY": FX})
