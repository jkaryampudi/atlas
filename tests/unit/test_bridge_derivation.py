"""ADR-0006 price-derivation math (atlas.dcp.trading.bridge.derive_prices),
pinned by hand over small wilder_atr series — the 2xATR stop vs the -10%
floor is exercised BOTH ways, plus the deterministic evidence signal ids."""
import uuid
from decimal import Decimal

from atlas.dcp.indicators.core import wilder_atr
from atlas.dcp.trading.bridge import derive_prices, evidence_signal_id


def _atr14(h: float, lo: float, c: float, n: int = 15) -> float:
    """Last Wilder ATR(14) over n identical bars (constant-bar series)."""
    out = wilder_atr([h] * n, [lo] * n, [c] * n, period=14)[-1]
    assert out is not None
    return out


def test_calm_series_uses_the_atr_stop():
    """Calm: 15 bars h=101, l=99, c=100. Every TR = max(101-99=2,
    |101-100|=1, |99-100|=1) = 2 (first bar TR = h-l = 2 too), so the seed
    mean and every Wilder step are exactly 2 -> ATR14 = 2.0.
      2 x ATR = 4  <  10% of entry = 10  ->  the ATR stop binds:
      stop   = 100 - 4          = 96.000000
      target = 100 + 2*(100-96) = 108.000000
    """
    atr = _atr14(101, 99, 100)
    assert atr == 2.0
    stop, target = derive_prices(Decimal("100"), atr)
    assert stop == Decimal("96.000000")
    assert target == Decimal("108.000000")


def test_violent_series_clamps_to_the_floor():
    """Violent: 15 bars h=104, l=97, c=100. Every TR = max(104-97=7,
    |104-100|=4, |97-100|=3) = 7 -> ATR14 = 7.0.
      2 x ATR = 14  >  10% of entry = 10  ->  the -10% floor clamps:
      stop   = max(100-14, 100*0.90) = 90.000000
      target = 100 + 2*(100-90)      = 120.000000
    """
    atr = _atr14(104, 97, 100)
    assert atr == 7.0
    stop, target = derive_prices(Decimal("100"), atr)
    assert stop == Decimal("90.000000")
    assert target == Decimal("120.000000")


def test_float_atr_crosses_into_decimal_via_str():
    """Non-trivial decimals: entry 52.37, ATR 1.2345 (float).
      2 x ATR = 2.469; floor = 52.37 * 0.90 = 47.133
      52.37 - 2.469 = 49.901 > 47.133 -> ATR stop binds
      stop   = 49.901000 exactly (Decimal(str(...)), no float artifacts)
      target = 52.37 + 2*(52.37 - 49.901) = 52.37 + 4.938 = 57.308000
    """
    stop, target = derive_prices(Decimal("52.37"), 1.2345)
    assert stop == Decimal("49.901000")
    assert target == Decimal("57.308000")


def test_exact_boundary_is_the_floor_value():
    """2 x ATR exactly 10% of entry: both candidates equal 90 — one stop."""
    stop, target = derive_prices(Decimal("100"), 5.0)
    assert stop == Decimal("90.000000")
    assert target == Decimal("120.000000")


def test_evidence_signal_ids_are_deterministic_uuid5():
    ref = "bars:ZBRA:2026-07-13"
    expected = uuid.uuid5(uuid.NAMESPACE_URL, f"atlas:evidence:{ref}")
    assert evidence_signal_id(ref) == expected
    assert evidence_signal_id(ref) == evidence_signal_id(ref)   # stable
    assert evidence_signal_id("other") != expected
