"""Regime classifier v1 (IPD S3 overlay): bull / bear / high_vol / neutral.

Strictly causal: every value at day t uses data up to t only. high_vol overrides
direction (the overlay's job is to pull risk in chaos regardless of trend).
"""
from __future__ import annotations

import math
import statistics
from typing import Literal

from atlas.dcp.backtest.engine import OBar
from atlas.dcp.indicators.core import sma

Regime = Literal["bull", "bear", "high_vol", "neutral"]

TREND_WINDOW = 100
VOL_WINDOW = 20
VOL_MULT = 1.6          # high_vol when trailing vol > 1.6 × expanding median vol


def classify_series(bars: list[OBar]) -> list[Regime]:
    closes = [b.close for b in bars]
    trend = sma(closes, TREND_WINDOW)
    out: list[Regime] = []
    vols: list[float] = []
    for i in range(len(bars)):
        if i < max(TREND_WINDOW, VOL_WINDOW + 1):
            out.append("neutral")
            if i >= VOL_WINDOW:
                rets = [closes[j] / closes[j - 1] - 1 for j in range(i - VOL_WINDOW + 1, i + 1)]
                vols.append(statistics.pstdev(rets) * math.sqrt(252))
            continue
        rets = [closes[j] / closes[j - 1] - 1 for j in range(i - VOL_WINDOW + 1, i + 1)]
        vol = statistics.pstdev(rets) * math.sqrt(252)
        vols.append(vol)
        med = statistics.median(vols)
        t = trend[i]
        if med > 0 and vol > VOL_MULT * med:
            out.append("high_vol")
        elif t is not None and closes[i] > t:
            out.append("bull")
        elif t is not None and closes[i] < t:
            out.append("bear")
        else:
            out.append("neutral")
    return out
