import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from fixtures.synthetic import _mk, regime_series  # noqa: E402

from atlas.dcp.backtest.engine import OBar  # noqa: E402
from atlas.dcp.signals.regime.v1 import classify_series  # noqa: E402


def _acc(regs, lo, hi, want):
    seg = regs[lo:hi]
    return sum(1 for r in seg if r == want) / len(seg)


def test_block_interiors_labelled_correctly():
    regs = classify_series(regime_series())
    assert _acc(regs, 140, 160, "bull") >= 0.95
    assert _acc(regs, 460, 480, "bull") >= 0.95
    assert _acc(regs, 260, 320, "bear") >= 0.95
    assert _acc(regs, 580, 640, "bear") >= 0.95


def test_warmup_is_neutral():
    regs = classify_series(regime_series())
    assert set(regs[:100]) == {"neutral"}


def test_strictly_causal():
    bars = regime_series()
    corrupted = bars[:900] + [OBar(1.0, 1.0, 1.0, 1.0, 1) for _ in bars[900:]]
    assert classify_series(bars)[:900] == classify_series(corrupted)[:900]


def test_high_vol_overrides_direction():
    rng = random.Random(5)
    px, closes = 100.0, []
    for i in range(400):
        vol = 0.006 if i < 250 else 0.045          # calm, then chaos
        px *= math.exp(0.0015 + vol * rng.gauss(0, 1))
        closes.append(px)
    regs = classify_series(_mk(closes, 5))
    assert _acc(regs, 300, 400, "high_vol") >= 0.7
