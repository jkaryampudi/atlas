from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlas.dcp.portfolio.snapshot import Holding, compute_snapshot

FX = {"USD": Decimal("1.52"), "AUD": Decimal("1")}


def test_hand_computed_nav_to_the_cent():
    # Phase 1 exit criterion: matches hand calculation exactly.
    # 8 AVGO @ 172.40 USD * 1.52 = 2096.384 -> 2096.38 AUD (banker's rounding)
    # 10 NDIA @ 71.25 AUD = 712.50 AUD ; cash 97,000 AUD
    snap = compute_snapshot(
        cash_aud=Decimal("97000.00"),
        holdings=[Holding("AVGO", 8, "USD", Decimal("172.40")),
                  Holding("NDIA", 10, "AUD", Decimal("71.25"))],
        fx_to_aud=FX)
    assert snap.holdings_value_aud == Decimal("2808.88")
    assert snap.nav_aud == Decimal("99808.88")
    assert snap.weights["AVGO"] == Decimal("0.0210")
    assert snap.non_aud_exposure_pct == Decimal("0.0210")


def test_long_only_enforced():
    with pytest.raises(ValueError):
        compute_snapshot(cash_aud=Decimal("1000"),
                         holdings=[Holding("SPY", -1, "USD", Decimal("500"))],
                         fx_to_aud=FX)


def test_missing_fx_rate_raises():
    with pytest.raises(KeyError):
        compute_snapshot(cash_aud=Decimal("1000"),
                         holdings=[Holding("INFY", 1, "INR", Decimal("1500"))],
                         fx_to_aud=FX)


@given(qty=st.integers(min_value=0, max_value=10_000),
       price=st.decimals(min_value="0.01", max_value="10000", places=2),
       cash=st.decimals(min_value="1", max_value="1000000", places=2))
def test_weights_never_exceed_one_and_sum_bounded(qty, price, cash):
    snap = compute_snapshot(cash_aud=cash,
                            holdings=[Holding("SPY", qty, "USD", price)],
                            fx_to_aud=FX)
    for w in snap.weights.values():
        assert Decimal(0) <= w <= Decimal(1)
    assert snap.nav_aud == snap.cash_aud + snap.holdings_value_aud
