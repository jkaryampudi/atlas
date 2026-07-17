"""Pure attribution conventions (atlas/dcp/reporting/attribution.py):
flow-adjusted returns and the signed 55:15 core blend — hand-computed pins.

The convention under test (module docstring, stated once): flows occur at the
session open, so ret = (V - P - F_in + F_out) / (P + F_in). A buy's cost joins
the base; a sale's proceeds are credited back to the numerator and its capital
leaves the base. No prior observation or no base -> None, never a fake 0.
"""
from __future__ import annotations

from decimal import Decimal

from atlas.dcp.reporting.attribution import (
    core_blend_return,
    flow_adjusted_return,
)
from atlas.dcp.trading.core_allocation import CORE_TARGETS


def test_quiet_day_is_a_plain_return():
    # (110 - 100) / 100, no flows
    assert flow_adjusted_return(Decimal("110"), Decimal("100"),
                                Decimal(0), Decimal(0)) == Decimal("0.10000000")


def test_buy_day_books_only_the_post_fill_move():
    """Sleeve was empty (P=0); a 1200 buy marks to 1260 by the close: the
    1200 is capital moving cash->sleeve, the 60 is the day's performance.
    ret = (1260 - 0 - 1200 + 0) / (0 + 1200) = 0.05 — a naive value diff
    would have booked +infinity%."""
    assert flow_adjusted_return(Decimal("1260"), Decimal("0"),
                                Decimal("1200"), Decimal(0)) \
        == Decimal("0.05000000")


def test_buy_into_held_sleeve_expands_the_base():
    # P=1000 marks to 2260 after a 1200 buy: pnl = 2260-1000-1200 = 60,
    # base = 1000+1200 = 2200 -> 60/2200
    assert flow_adjusted_return(Decimal("2260"), Decimal("1000"),
                                Decimal("1200"), Decimal(0)) \
        == Decimal("0.02727273")   # 60/2200 = 0.0272727... 8dp half-even


def test_full_liquidation_grades_proceeds_vs_prior_mark():
    """All lots sold: V=0, proceeds 7687.30 vs yesterday's 8343 mark.
    ret = (0 - 8343 + 7687.30) / 8343 = -655.70/8343 — the sale's capital
    left at the open, so the base stays the prior value alone."""
    assert flow_adjusted_return(Decimal("0"), Decimal("8343"),
                                Decimal(0), Decimal("7687.30")) \
        == Decimal("-0.07859283")


def test_no_prior_observation_is_none_not_zero():
    assert flow_adjusted_return(Decimal("100"), None,
                                Decimal(0), Decimal(0)) is None


def test_zero_base_is_none_not_zero():
    # empty sleeve, no buys: nothing was invested, so there is no return
    assert flow_adjusted_return(Decimal("0"), Decimal("0"),
                                Decimal(0), Decimal(0)) is None


def test_core_blend_is_the_signed_targets_renormalized():
    """(0.55*r_spy + 0.15*r_inda) / 0.70 — the ADR-0012 signed weights, and
    the weights COME from core_allocation.CORE_TARGETS (single source)."""
    assert CORE_TARGETS == {"SPY": Decimal("0.55"), "INDA": Decimal("0.15")}
    # hand: (0.55*0.01 + 0.15*0.03)/0.70 = 0.01/0.70 = 0.01428571...
    assert core_blend_return(Decimal("0.01"), Decimal("0.03")) \
        == Decimal("0.01428571")
    # equal legs pass straight through (a blend of x and x is x)
    assert core_blend_return(Decimal("0.02"), Decimal("0.02")) \
        == Decimal("0.02000000")


def test_core_blend_refuses_a_one_legged_benchmark():
    assert core_blend_return(None, Decimal("0.01")) is None
    assert core_blend_return(Decimal("0.01"), None) is None
    assert core_blend_return(None, None) is None
