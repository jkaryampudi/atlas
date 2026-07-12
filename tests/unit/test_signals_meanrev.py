"""Mean-reversion v1 (Connors RSI(2) long-only): hand-verified entry/exit
golden pins on constructed series, drift pins on the shared synthetic fixture,
and the structural no-look-ahead property in the house style (mirrors
test_backtest_engine.py).

Hand-computed fixture arithmetic (Wilder RSI, period 2):
  ramp 100.0 + 0.1*i for i in 0..200 -> every change is a gain, avg_loss = 0,
  RSI = 100, SMA200 at i=200 is 100 + 0.1*mean(1..200) = 110.05 << close 120.
  One -1.0 drop to 119: avg_gain = (0.1 + 0)/2 = 0.05, avg_loss = (0 + 1)/2
  = 0.5, RSI = 100*0.05/0.55 = 9.0909 < 10 and 119 > SMA200 -> ENTRY.
  RSI-70 exit threshold: gain g with (ag + g)/2 >= (7/3)*(al/2) i.e.
  g = (7*0.5 - 3*0.05)/3 = 33.5/30 -> target = 119 + 33.5/30 = 120.11667;
  a close exactly there gives ag' = (0.05 + 33.5/30)/2, al' = 0.25,
  RSI' = 70.0 exactly."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from fixtures.synthetic import regime_series  # noqa: E402

from atlas.dcp.backtest.engine import CostModel, OBar, run_backtest  # noqa: E402
from atlas.dcp.signals.meanrev.v1 import (  # noqa: E402
    MAX_HOLD,
    SMA_FILTER,
    SPEC,
    meanrev_v1,
)

COSTS = CostModel()
TARGET = 119.0 + 33.5 / 30.0


def flat(c: float) -> OBar:
    return OBar(open=c, high=c, low=c, close=c, volume=1.0)


def ramp_then_drop() -> list[OBar]:
    return [flat(100.0 + 0.1 * i) for i in range(201)] + [flat(119.0)]


def test_spec_is_textbook_and_family_named():
    assert SPEC["family"] == "meanrev"
    assert (SPEC["rsi_period"], SPEC["entry_rsi"], SPEC["exit_rsi"],
            SPEC["sma_filter"], SPEC["max_hold"]) == (2, 10.0, 70.0, 200, 10)


def test_warmup_returns_none():
    assert meanrev_v1([flat(100.0)] * (SMA_FILTER - 1)) is None


def test_entry_signal_hand_verified():
    intent = meanrev_v1(ramp_then_drop())
    assert intent is not None
    assert intent.stop == 0.0                       # Connors: no protective stop
    assert intent.target == pytest.approx(TARGET)   # the RSI-70 price threshold
    assert intent.time_stop == MAX_HOLD


def test_no_signal_when_rsi_not_oversold():
    """The pure ramp has RSI(2) = 100 — above the SMA filter is not enough."""
    assert meanrev_v1([flat(100.0 + 0.1 * i) for i in range(201)]) is None


def test_no_signal_below_sma_filter():
    """A steady downtrend has RSI(2) = 0 < 10 but close < SMA200 -> no entry
    (the Connors regime filter)."""
    assert meanrev_v1([flat(200.0 - 0.1 * i) for i in range(201)]) is None


def test_engine_target_exit_at_rsi70_threshold_golden():
    """Signal at bar 201, entry at bar 202 open (119*1.001); the same bar's
    high 120.2 >= 120.11667 lifts RSI(2) to exactly 70 -> target exit."""
    bars = ramp_then_drop()
    bars.append(OBar(open=119.0, high=120.2, low=119.0, close=120.0, volume=1.0))
    bars += [flat(120.0)] * 3
    r = run_backtest(bars, meanrev_v1, COSTS, start_i=1, end_i=len(bars))
    assert [(t.entry_i, t.exit_i, t.reason) for t in r.trades] == \
        [(202, 202, "target")]
    assert r.trades[0].entry == pytest.approx(119.0 * 1.001)
    assert r.trades[0].exit == pytest.approx(TARGET * 0.999)


def test_engine_time_exit_after_max_hold_golden():
    """If the bounce never comes (flat at 119, RSI stays 9.09), the position is
    closed by the 10-bar time stop, never by the unused 0.0 stop."""
    bars = ramp_then_drop() + [flat(119.0)] * 13
    r = run_backtest(bars, meanrev_v1, COSTS, start_i=1, end_i=len(bars))
    assert r.trades[0].entry_i == 202
    assert (r.trades[0].exit_i, r.trades[0].reason) == (202 + MAX_HOLD, "time")


def test_no_look_ahead_is_structural():
    """Mutating the FUTURE must not change any past decision (house property
    test, mirrors test_backtest_engine.py)."""
    bars = regime_series()
    cut = 700
    corrupted = bars[:cut] + [OBar(1.0, 1.0, 1.0, 1.0, 1) for _ in bars[cut:]]
    decisions_a, decisions_b = [], []

    def spy(record):
        def s(hist):
            out = meanrev_v1(hist)
            record.append((len(hist), out is not None))
            return out
        return s

    run_backtest(bars, spy(decisions_a), COSTS, start_i=60, end_i=len(bars))
    run_backtest(corrupted, spy(decisions_b), COSTS, start_i=60, end_i=len(bars))
    upto = [d for d in decisions_a if d[0] <= cut]
    assert upto == [d for d in decisions_b if d[0] <= cut]


def test_costs_strictly_reduce_returns():
    bars = regime_series()
    free = run_backtest(bars, meanrev_v1, CostModel(0, 0), start_i=60, end_i=len(bars))
    paid = run_backtest(bars, meanrev_v1, COSTS, start_i=60, end_i=len(bars))
    assert free.total_return > paid.total_return


def test_golden_regression_pins():
    """Any drift in engine/strategy/fixture behaviour fails here (Doc 07 §3)."""
    r = run_backtest(regime_series(), meanrev_v1, COSTS, start_i=60, end_i=1200)
    assert r.total_return == pytest.approx(-0.21801054229210914)
    assert r.sharpe == pytest.approx(-1.110146973897122)
    assert r.n_trades == 22
    assert r.max_drawdown == pytest.approx(-0.23582232520460777)
