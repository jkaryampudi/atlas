"""L8 correlation feed (Doc 04 §3 L8): golden Pearson values on synthetic
return series, and the fail-closed pairing logic on synthetic close maps.
The wired market.price_bars_daily query path is exercised in
tests/integration/test_correlations_pg.py."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from atlas.dcp.risk.correlations import (
    MIN_OVERLAP_RETURNS,
    _pair_correlation,
    pairwise_correlation,
)


# ---------------------------------------------------------------- pure Pearson

def test_pearson_golden_hand_computed():
    # a = [0.01, 0.02, 0.03], b = [0.01, 0.03, 0.02]; means 0.02 / 0.02.
    # deviations da = [-0.01, 0, 0.01], db = [-0.01, 0.01, 0].
    # sum(da*db) = 0.0001; sum(da^2) = sum(db^2) = 0.0002.
    # r = 0.0001 / sqrt(0.0002 * 0.0002) = 0.5
    assert pairwise_correlation([0.01, 0.02, 0.03],
                                [0.01, 0.03, 0.02]) == Decimal("0.5000")


def test_pearson_exact_affine_relations():
    a = [0.01, 0.02, -0.01, 0.03]
    # b = 2a: perfectly correlated
    assert pairwise_correlation(a, [2 * x for x in a]) == Decimal("1.0000")
    # b = -a + 0.01: perfectly anti-correlated
    assert pairwise_correlation(a, [0.01 - x for x in a]) == Decimal("-1.0000")


def test_pearson_accepts_decimal_returns():
    a = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]
    b = [Decimal("0.01"), Decimal("0.03"), Decimal("0.02")]
    assert pairwise_correlation(a, b) == Decimal("0.5000")


def test_pearson_rejects_degenerate_input():
    with pytest.raises(ValueError, match="equal length"):
        pairwise_correlation([0.01, 0.02], [0.01])
    with pytest.raises(ValueError, match="at least 2"):
        pairwise_correlation([0.01], [0.02])
    with pytest.raises(ValueError, match="zero variance"):
        pairwise_correlation([0.01, 0.01, 0.01], [0.01, 0.02, 0.03])


# ------------------------------------------------------- pairing (fail-closed)

def _closes(n, start=100.0, phase=0, base_date=date(2025, 1, 6)):
    """n daily closes alternating x1.02 / x0.99 multipliers. phase=1 swaps the
    multiplier order, making returns b = 0.01 - a — an exact affine relation
    with slope -1, so Pearson is exactly -1 against phase=0."""
    closes, c = {}, start
    for i in range(n):
        if i:
            c *= 1.02 if (i + phase) % 2 else 0.99
        closes[base_date + timedelta(days=i)] = Decimal(str(round(c, 6)))
    return closes


def test_pair_correlation_computes_on_sufficient_overlap():
    a, b = _closes(90), _closes(90, phase=1)
    assert _pair_correlation(a, b) == Decimal("-1.0000")


def test_pair_correlation_fails_closed_on_thin_overlap():
    # 60 returns need 61 aligned closes; 60 closes -> 59 returns -> worst case.
    # The anti-correlated construction would read -1.0000 if it were computed,
    # so Decimal("1") proves fail-closed, not a computed value.
    assert MIN_OVERLAP_RETURNS == 60
    a, b = _closes(60), _closes(60, phase=1)
    assert _pair_correlation(a, b) == Decimal("1")
    assert _pair_correlation(_closes(61), _closes(61, phase=1)) == Decimal("-1.0000")
    # disjoint calendars: zero overlap
    assert _pair_correlation(a, _closes(90, base_date=date(2020, 1, 1))) == Decimal("1")


def test_pair_correlation_fails_closed_on_degenerate_prices():
    a = _closes(90)
    # zero variance: constant closes have undefined correlation
    flat = {d: Decimal("100") for d in a}
    assert _pair_correlation(a, flat) == Decimal("1")
    # non-positive close: returns are meaningless
    bad = dict(a)
    bad[sorted(bad)[10]] = Decimal("0")
    assert _pair_correlation(a, bad) == Decimal("1")
