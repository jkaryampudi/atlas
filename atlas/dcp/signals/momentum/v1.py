"""Momentum v1 (spec per IPD S1): price > SMA20 > SMA50, positive 20d return,
volume confirmation. Stop 2×ATR14, target 4×ATR14, 40-day time stop.
Same code path serves backtest and production (ADR-0002 #4).
"""
from __future__ import annotations

from atlas.dcp.backtest.engine import Intent, OBar
from atlas.dcp.indicators.core import atr, rolling_return, sma

SMA_FAST, SMA_SLOW, RET_WINDOW = 20, 50, 20
STOP_ATR, TARGET_ATR = 2.0, 4.0
TIME_STOP, VOL_MULT = 40, 1.0
SPEC: dict[str, object] = {"family": "momentum", "name": "trend_rs_vol",
    "version": "1.0.0", "sma_fast": SMA_FAST, "sma_slow": SMA_SLOW,
    "ret_window": RET_WINDOW, "stop_atr": STOP_ATR, "target_atr": TARGET_ATR,
    "time_stop": TIME_STOP, "vol_mult": VOL_MULT}


def momentum_v1(bars: list[OBar]) -> Intent | None:
    n = len(bars)
    if n < SMA_SLOW + 1:
        return None
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    vols = [b.volume for b in bars]
    f = sma(closes, SMA_FAST)[-1]
    s = sma(closes, SMA_SLOW)[-1]
    r = rolling_return(closes, RET_WINDOW)[-1]
    a = atr(highs, lows, closes, 14)[-1]
    v_avg = sma(vols, 20)[-1]
    if f is None or s is None or r is None or a is None or v_avg is None:
        return None
    c = closes[-1]
    if c > f > s and r > 0 and vols[-1] >= VOL_MULT * v_avg:
        return Intent(stop=c - STOP_ATR * a, target=c + TARGET_ATR * a,
                      time_stop=TIME_STOP)
    return None
