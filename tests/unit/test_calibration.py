import pytest

from atlas.dcp.learning.calibration import (WEIGHT_MAX, WEIGHT_MIN, Forecast,
                                            brier_score, conviction_weight)


def test_brier_perfect_high_conviction_hits():
    fs = [Forecast("HIGH", True)] * 10
    assert brier_score(fs) == pytest.approx((0.75 - 1.0) ** 2)


def test_overconfident_agent_loses_weight_slowly_then_more():
    misses = [Forecast("HIGH", False)] * 5           # tiny sample
    w_small = conviction_weight(misses)
    assert 0.75 < w_small < 1.0                      # shrinkage: stays well above floor
    misses_big = [Forecast("HIGH", False)] * 100     # persistent overconfidence
    w_big = conviction_weight(misses_big)
    assert w_big == WEIGHT_MIN                       # clipped floor, never silenced


def test_well_calibrated_agent_gains_bounded_weight():
    hits = [Forecast("HIGH", True)] * 200
    assert conviction_weight(hits) <= WEIGHT_MAX


def test_empty_history_keeps_prev_weight():
    assert conviction_weight([], prev_weight=1.2) == 1.2
