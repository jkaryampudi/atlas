"""Trend v1: long-term trend filter — long when close > SMA(200) with a 2%
hysteresis band (enter above SMA*1.02, exit below SMA*0.98), else flat.

Textbook parameters, NO parameter search: the 200-day SMA timing rule follows
Faber (2007), "A Quantitative Approach to Tactical Asset Allocation" (10-month
SMA ≈ 200 trading days, evaluated monthly); the ±2% hysteresis band is the
classic whipsaw filter on the 200-day moving average (cf. Siegel, "Stocks for
the Long Run", band-filtered MA timing). Long-only per the Atlas mandate.

Engine mapping (exits are frozen at entry — structural, see engine.py): the
hysteresis state (long between the bands only if already long) is replayed from
bar 0 as a pure function of bars[:i+1] — no stored state, so no-look-ahead
stays structural. The exit band 0.98*SMA200 at (re-)entry is posted as the
stop, and the position is re-evaluated every REEVAL=21 bars (one trading month,
Faber's evaluation cadence) via the engine time stop; each refresh pays a full
round trip of costs, a conservative (cost-adding) artifact of the mapping.
Same code path serves backtest and production (ADR-0002 #4).
"""
from __future__ import annotations

import math

from atlas.dcp.backtest.engine import Intent, OBar

SMA_WINDOW = 200
ENTER_BAND, EXIT_BAND = 1.02, 0.98
REEVAL = 21
SPEC: dict[str, object] = {"family": "trend", "name": "sma200_hysteresis",
    "version": "1.0.0", "sma_window": SMA_WINDOW, "enter_band": ENTER_BAND,
    "exit_band": EXIT_BAND, "reeval": REEVAL,
    "provenance": "textbook (Faber 2007 / Siegel band filter); no search"}


def _replay_state(closes: list[float]) -> tuple[bool, float]:
    """Replay the hysteresis state machine over the full history. Returns
    (long?, current SMA200). Incremental running sum keeps this O(n)."""
    long_state = False
    run = 0.0
    s = 0.0
    for i, c in enumerate(closes):
        run += c
        if i >= SMA_WINDOW:
            run -= closes[i - SMA_WINDOW]
        if i < SMA_WINDOW - 1:
            continue
        s = run / SMA_WINDOW
        if not long_state and c > s * ENTER_BAND:
            long_state = True
        elif long_state and c < s * EXIT_BAND:
            long_state = False
    return long_state, s


def trend_v1(bars: list[OBar]) -> Intent | None:
    if len(bars) < SMA_WINDOW:
        return None
    long_state, s = _replay_state([b.close for b in bars])
    if long_state:
        return Intent(stop=s * EXIT_BAND, target=math.inf, time_stop=REEVAL)
    return None
