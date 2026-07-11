"""Seeded synthetic series: regime-switching trend (momentum-friendly, beats B&H
because downtrends punish holding) and a pure random walk (nothing to find)."""
from __future__ import annotations

import math
import random

from atlas.dcp.backtest.engine import OBar


def _mk(closes: list[float], seed: int) -> list[OBar]:
    rng = random.Random(seed + 1)
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        hi = max(o, c) * (1 + rng.uniform(0.001, 0.006))
        lo = min(o, c) * (1 - rng.uniform(0.001, 0.006))
        bars.append(OBar(open=o, high=hi, low=lo, close=c, volume=1_000_000))
    return bars


def regime_series(n: int = 1200, seed: int = 42) -> list[OBar]:
    rng = random.Random(seed)
    px, closes = 100.0, []
    for i in range(n):
        block = (i // 160) % 2               # 160d up, 160d down, repeat
        drift = 0.0024 if block == 0 else -0.0018
        px *= math.exp(drift + 0.008 * rng.gauss(0, 1))
        closes.append(px)
    return _mk(closes, seed)


def random_walk(n: int = 1200, seed: int = 99) -> list[OBar]:
    rng = random.Random(seed)
    px, closes = 100.0, []
    for _ in range(n):
        px *= math.exp(0.012 * rng.gauss(0, 1))
        closes.append(px)
    return _mk(closes, seed)
