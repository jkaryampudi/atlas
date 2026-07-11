import pytest

from atlas.dcp.indicators.core import atr, rolling_return, sma


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
