"""Trend v1 (SMA200 + 2% hysteresis band, Faber-style): hand-verified
entry/exit golden pins on constructed series, drift pins on the shared
synthetic fixture, and the structural no-look-ahead property in the house
style (mirrors test_backtest_engine.py)."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from fixtures.synthetic import regime_series  # noqa: E402

from atlas.dcp.backtest.engine import CostModel, OBar, run_backtest  # noqa: E402
from atlas.dcp.signals.trend.v1 import (  # noqa: E402
    EXIT_BAND,
    REEVAL,
    SMA_WINDOW,
    SPEC,
    trend_v1,
)

COSTS = CostModel()


def flat(c: float) -> OBar:
    return OBar(open=c, high=c, low=c, close=c, volume=1.0)


def test_spec_is_textbook_and_family_named():
    assert SPEC["family"] == "trend"
    assert (SPEC["sma_window"], SPEC["enter_band"], SPEC["exit_band"]) == \
        (200, 1.02, 0.98)


def test_warmup_returns_none():
    assert trend_v1([flat(100.0)] * (SMA_WINDOW - 1)) is None


def test_entry_signal_and_posted_exit_band():
    """Hand-verified: 200 flat bars at 100, then a close at 103.
    SMA200 = (199*100 + 103)/200 = 100.015; entry band = 102.0153 < 103 -> long.
    Posted stop = 0.98 * 100.015 = 98.0147; no target; monthly re-evaluation."""
    intent = trend_v1([flat(100.0)] * SMA_WINDOW + [flat(103.0)])
    assert intent is not None
    assert intent.stop == pytest.approx(EXIT_BAND * 100.015)
    assert math.isinf(intent.target)
    assert intent.time_stop == REEVAL


def test_no_entry_below_band():
    """101.5 < 1.02 * 100.0075 = 102.00765 -> stays flat."""
    assert trend_v1([flat(100.0)] * SMA_WINDOW + [flat(101.5)]) is None


def test_hysteresis_state_is_path_dependent():
    """Inside the band (98%..102% of SMA) the state is held, not recomputed:
    the same final close of 101 is long only if 103 was touched first."""
    long_path = [flat(100.0)] * SMA_WINDOW + [flat(103.0), flat(101.0)]
    flat_path = [flat(100.0)] * SMA_WINDOW + [flat(101.0), flat(101.0)]
    assert trend_v1(long_path) is not None
    assert trend_v1(flat_path) is None


def test_exit_below_band_goes_flat():
    """At the final bar SMA200 = (198*100 + 103 + 97)/200 = 100.0 exactly;
    97 < 98.0 = exit band -> flat."""
    assert trend_v1([flat(100.0)] * SMA_WINDOW + [flat(103.0), flat(97.0)]) is None


def test_engine_stop_exit_at_frozen_band_golden():
    """Hand-verified engine trace: signal at bar 200 (103 > 102.0153), entry
    next open (bar 201) at 103*1.001, stop frozen at 0.98*100.015 = 98.0147,
    hit by the 97 bar at index 205; the replayed state also flips flat there
    (97 < 0.98 * SMA), so exactly one trade."""
    bars = [flat(100.0)] * SMA_WINDOW + [flat(103.0)] * 5 + [flat(97.0)] * 5
    r = run_backtest(bars, trend_v1, COSTS, start_i=1, end_i=len(bars))
    assert r.n_trades == 1
    t = r.trades[0]
    assert (t.entry_i, t.exit_i, t.reason) == (201, 205, "stop")
    assert t.entry == pytest.approx(103.0 * 1.001)
    assert t.exit == pytest.approx(EXIT_BAND * 100.015 * 0.999)


def test_monthly_reeval_reenters_inside_band_golden():
    """After the monthly time stop the position is re-entered while the close
    (101) sits INSIDE the band — only the replayed hysteresis state allows
    that; a memoryless close > 1.02*SMA rule would never re-enter at 101."""
    bars = [flat(100.0)] * SMA_WINDOW + [flat(103.0)] + [flat(101.0)] * 49
    r = run_backtest(bars, trend_v1, COSTS, start_i=1, end_i=len(bars))
    assert [(t.entry_i, t.exit_i, t.reason) for t in r.trades] == \
        [(201, 222, "time"), (223, 244, "time")]


def test_no_look_ahead_is_structural():
    """Mutating the FUTURE must not change any past decision (house property
    test, mirrors test_backtest_engine.py)."""
    bars = regime_series()
    cut = 700
    corrupted = bars[:cut] + [OBar(1.0, 1.0, 1.0, 1.0, 1) for _ in bars[cut:]]
    decisions_a, decisions_b = [], []

    def spy(record):
        def s(hist):
            out = trend_v1(hist)
            record.append((len(hist), out is not None))
            return out
        return s

    run_backtest(bars, spy(decisions_a), COSTS, start_i=60, end_i=len(bars))
    run_backtest(corrupted, spy(decisions_b), COSTS, start_i=60, end_i=len(bars))
    upto = [d for d in decisions_a if d[0] <= cut]
    assert upto == [d for d in decisions_b if d[0] <= cut]


def test_costs_strictly_reduce_returns():
    bars = regime_series()
    free = run_backtest(bars, trend_v1, CostModel(0, 0), start_i=60, end_i=len(bars))
    paid = run_backtest(bars, trend_v1, COSTS, start_i=60, end_i=len(bars))
    assert free.total_return > paid.total_return


def test_golden_regression_pins():
    """Any drift in engine/strategy/fixture behaviour fails here (Doc 07 §3)."""
    r = run_backtest(regime_series(), trend_v1, COSTS, start_i=60, end_i=1200)
    assert r.total_return == pytest.approx(-0.09086623621181122)
    assert r.sharpe == pytest.approx(-0.19757882149980135)
    assert r.n_trades == 30
    assert r.max_drawdown == pytest.approx(-0.18592326158785055)
