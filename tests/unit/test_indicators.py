import pytest

from atlas.dcp.indicators.core import (
    atr,
    rolling_return,
    rsi,
    sma,
    wilder_atr,
    wilder_avg_gain_loss,
)


def test_sma_hand_values():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_sma_rejects_bad_window():
    with pytest.raises(ValueError):
        sma([1.0], 0)


def test_rolling_return():
    out = rolling_return([100, 110, 121], 1)
    assert out[1] == pytest.approx(0.10)
    assert out[2] == pytest.approx(0.10)


def test_atr_positive_and_lagged():
    a = atr([10, 11, 12], [9, 10, 11], [9.5, 10.5, 11.5], window=2)
    assert a[0] is None and a[-1] is not None and a[-1] > 0


def test_wilder_atr_hand_pins():
    """Golden pins, verified by hand (ADR-0006 Wilder ATR):
      TR0 = h-l           = 12 - 8                              = 4
      TR1 = max(11-9,  |11-10|,   |9-10|)   = max(2, 1,   1)    = 2
      TR2 = max(14-10, |14-10.5|, |10-10.5|) = max(4, 3.5, 0.5) = 4
      seed ATR (i=2) = (4 + 2 + 4) / 3                          = 10/3
      TR3 = max(13-11, |13-13|,   |11-13|)  = max(2, 0,   2)    = 2
      ATR3 = (10/3 * 2 + 2) / 3                                 = 26/9
      TR4 = max(15-12, |15-12|,   |12-12|)  = max(3, 3,   0)    = 3
      ATR4 = (26/9 * 2 + 3) / 3                                 = 79/27
    """
    out = wilder_atr([12, 11, 14, 13, 15], [8, 9, 10, 11, 12],
                     [10, 10.5, 13, 12, 14], period=3)
    assert out[0] is None and out[1] is None       # None until warm
    assert out[2] == pytest.approx(10 / 3)
    assert out[3] == pytest.approx(26 / 9)
    assert out[4] == pytest.approx(79 / 27)


def test_wilder_atr_gap_true_ranges():
    """TR must use the previous close on gaps, both directions:
      gap UP:   prev close 9.5,  bar (h=15, l=14):
                TR = max(1, |15-9.5|=5.5, |14-9.5|=4.5) = 5.5
                ATR(2) at i=1 = (TR0 + TR1)/2 = (0.5 + 5.5)/2  = 3.0
      gap DOWN: prev close 9.8,  bar (h=8, l=7.5):
                TR = max(0.5, |8-9.8|=1.8, |7.5-9.8|=2.3) = 2.3
                ATR(2) at i=1 = (1.0 + 2.3)/2                  = 1.65
    """
    up = wilder_atr([10, 15], [9.5, 14], [9.5, 14.5], period=2)
    assert up[-1] == pytest.approx(3.0)
    down = wilder_atr([10, 8], [9, 7.5], [9.8, 7.6], period=2)
    assert down[-1] == pytest.approx((1.0 + 2.3) / 2)


def test_rsi_hand_pins():
    """Golden pins, verified by hand (Wilder RSI, period 2):
      closes [10, 11, 10.5, 10.5, 12] -> changes +1, -0.5, 0, +1.5
      seed (i=2): ag = (1 + 0)/2 = 0.5,  al = (0 + 0.5)/2 = 0.25
                  RSI = 100 * 0.5 / 0.75                       = 66.667
      i=3 (0):    ag = (0.5 + 0)/2 = 0.25, al = (0.25 + 0)/2 = 0.125
                  RSI = 100 * 0.25 / 0.375                     = 66.667
      i=4 (+1.5): ag = (0.25 + 1.5)/2 = 0.875, al = 0.0625
                  RSI = 100 * 0.875 / 0.9375                   = 93.333
    """
    out = rsi([10.0, 11.0, 10.5, 10.5, 12.0], period=2)
    assert out[0] is None and out[1] is None       # None until warm
    assert out[2] == pytest.approx(200.0 / 3.0)
    assert out[3] == pytest.approx(200.0 / 3.0)
    assert out[4] == pytest.approx(100.0 * 0.875 / 0.9375)


def test_rsi_extremes_and_flat_convention():
    assert rsi([1.0, 2.0, 3.0, 4.0], period=2)[-1] == 100.0   # no losses
    assert rsi([4.0, 3.0, 2.0, 1.0], period=2)[-1] == 0.0     # no gains
    assert rsi([5.0, 5.0, 5.0, 5.0], period=2)[-1] == 50.0    # flat: both zero
    with pytest.raises(ValueError):
        rsi([1.0, 2.0], period=0)


def test_wilder_avg_gain_loss_matches_rsi_seed():
    """The exposed averages are the RSI internals: the i=2 seed above."""
    pair = wilder_avg_gain_loss([10.0, 11.0, 10.5], period=2)[-1]
    assert pair is not None
    assert pair[0] == pytest.approx(0.5)
    assert pair[1] == pytest.approx(0.25)


def test_wilder_atr_warmup_and_bad_period():
    assert wilder_atr([10, 11], [9, 10], [9.5, 10.5], period=3) == [None, None]
    assert wilder_atr([], [], [], period=14) == []
    with pytest.raises(ValueError):
        wilder_atr([10.0], [9.0], [9.5], period=0)
