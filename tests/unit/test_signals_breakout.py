"""Breakout v1 (Donchian 55/20, Turtle System 2): hand-verified entry/exit
golden pins on constructed series, drift pins on the shared synthetic fixture,
and the structural no-look-ahead property in the house style (mirrors
test_backtest_engine.py).

Base fixture: 60 range bars (high 100, low 99, close 99.5), then a breakout
bar closing at 101 > 100 = max high of the prior 55 bars. Posted stop is the
min low of the last 20 bars = 99."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from fixtures.synthetic import regime_series  # noqa: E402

from atlas.dcp.backtest.engine import CostModel, OBar, run_backtest  # noqa: E402
from atlas.dcp.signals.breakout.v1 import (  # noqa: E402
    ENTRY_CHANNEL,
    REEVAL,
    SPEC,
    breakout_v1,
)

COSTS = CostModel()


def rng_bar() -> OBar:
    return OBar(open=99.5, high=100.0, low=99.0, close=99.5, volume=1.0)


def breakout_bar() -> OBar:
    return OBar(open=100.0, high=101.5, low=100.0, close=101.0, volume=1.0)


def test_spec_is_textbook_and_family_named():
    assert SPEC["family"] == "breakout"
    assert (SPEC["entry_channel"], SPEC["exit_channel"]) == (55, 20)


def test_warmup_returns_none():
    assert breakout_v1([rng_bar()] * ENTRY_CHANNEL) is None


def test_entry_on_55_bar_high_breakout():
    """close 101 > 100 = max high of the prior 55 bars -> long; posted stop is
    the 20-bar low (99); no target; re-evaluated every 20 bars."""
    intent = breakout_v1([rng_bar()] * 60 + [breakout_bar()])
    assert intent is not None
    assert intent.stop == pytest.approx(99.0)
    assert math.isinf(intent.target)
    assert intent.time_stop == REEVAL


def test_no_entry_at_or_below_channel_top():
    """A close AT the prior 55-bar high (100) is not a breakout."""
    touch = OBar(open=99.5, high=100.0, low=99.5, close=100.0, volume=1.0)
    assert breakout_v1([rng_bar()] * 60 + [touch]) is None


def test_exit_state_on_20_bar_low_break():
    """After the breakout, a low under the prior 20-bar low (99) flips the
    replayed state flat -> no signal."""
    pierce = OBar(open=101.0, high=101.0, low=98.0, close=98.5, volume=1.0)
    assert breakout_v1([rng_bar()] * 60 + [breakout_bar(), pierce]) is None


def test_engine_stop_exit_at_20_bar_low_golden():
    """Signal at bar 60, entry at bar 61 open (101*1.001), stop at 99; the
    crash bar at index 65 (low 97.5) hits it; the replayed state also exits
    there and 98 < the 55-bar channel top, so exactly one trade."""
    hold = OBar(open=101.0, high=101.5, low=100.5, close=101.0, volume=1.0)
    crash = OBar(open=98.0, high=98.5, low=97.5, close=98.0, volume=1.0)
    bars = [rng_bar()] * 60 + [breakout_bar()] + [hold] * 4 + [crash] * 3
    r = run_backtest(bars, breakout_v1, COSTS, start_i=1, end_i=len(bars))
    assert r.n_trades == 1
    t = r.trades[0]
    assert (t.entry_i, t.exit_i, t.reason) == (61, 65, "stop")
    assert t.entry == pytest.approx(101.0 * 1.001)
    assert t.exit == pytest.approx(99.0 * 0.999)


def test_engine_reeval_refreshes_trailing_stop_golden():
    """Turtle trailing exit via monthly refresh, hand-verified: rising bars
    61..81 (lows 100.2 .. 104.2) never break a 20-bar low, so the first trade
    ends on the 20-bar time stop at index 81; re-entry at 82 posts the
    REFRESHED stop min(lows[62..81]) = 100.4 (not the stale 99), which the
    crash bar at 83 (low 99.5) hits at 100.4, not 99."""
    rising = [OBar(open=100.0 + 0.2 * j, high=101.5 + 0.2 * j,
                   low=100.0 + 0.2 * j, close=101.0 + 0.2 * j, volume=1.0)
              for j in range(1, 22)]                       # indices 61..81
    top = OBar(open=105.4, high=105.6, low=105.2, close=105.4, volume=1.0)
    crash = OBar(open=100.0, high=100.5, low=99.5, close=100.0, volume=1.0)
    bars = [rng_bar()] * 60 + [breakout_bar()] + rising + [top, crash]
    r = run_backtest(bars, breakout_v1, COSTS, start_i=1, end_i=len(bars))
    assert [(t.entry_i, t.exit_i, t.reason) for t in r.trades] == \
        [(61, 81, "time"), (82, 83, "stop")]
    assert r.trades[0].exit == pytest.approx(105.2 * 0.999)   # close of bar 81
    assert r.trades[1].exit == pytest.approx(100.4 * 0.999)   # refreshed stop


def test_no_look_ahead_is_structural():
    """Mutating the FUTURE must not change any past decision (house property
    test, mirrors test_backtest_engine.py)."""
    bars = regime_series()
    cut = 700
    corrupted = bars[:cut] + [OBar(1.0, 1.0, 1.0, 1.0, 1) for _ in bars[cut:]]
    decisions_a, decisions_b = [], []

    def spy(record):
        def s(hist):
            out = breakout_v1(hist)
            record.append((len(hist), out is not None))
            return out
        return s

    run_backtest(bars, spy(decisions_a), COSTS, start_i=60, end_i=len(bars))
    run_backtest(corrupted, spy(decisions_b), COSTS, start_i=60, end_i=len(bars))
    upto = [d for d in decisions_a if d[0] <= cut]
    assert upto == [d for d in decisions_b if d[0] <= cut]


def test_costs_strictly_reduce_returns():
    bars = regime_series()
    free = run_backtest(bars, breakout_v1, CostModel(0, 0), start_i=60, end_i=len(bars))
    paid = run_backtest(bars, breakout_v1, COSTS, start_i=60, end_i=len(bars))
    assert free.total_return > paid.total_return


def test_golden_regression_pins():
    """Any drift in engine/strategy/fixture behaviour fails here (Doc 07 §3)."""
    r = run_backtest(regime_series(), breakout_v1, COSTS, start_i=60, end_i=1200)
    assert r.total_return == pytest.approx(1.2493866302058887)
    assert r.sharpe == pytest.approx(2.1415176139667773)
    assert r.n_trades == 25
    assert r.max_drawdown == pytest.approx(-0.09963905448419452)
