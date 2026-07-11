import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from fixtures.synthetic import regime_series  # noqa: E402

from atlas.dcp.backtest.engine import CostModel, OBar, run_backtest  # noqa: E402
from atlas.dcp.signals.momentum.v1 import momentum_v1  # noqa: E402


def test_no_look_ahead_is_structural():
    """Mutating the FUTURE must not change any past decision."""
    bars = regime_series()
    cut = 700
    corrupted = bars[:cut] + [OBar(1.0, 1.0, 1.0, 1.0, 1) for _ in bars[cut:]]
    decisions_a, decisions_b = [], []

    def spy(record):
        def s(hist):
            out = momentum_v1(hist)
            record.append((len(hist), out is not None))
            return out
        return s

    run_backtest(bars, spy(decisions_a), CostModel(), start_i=60, end_i=len(bars))
    run_backtest(corrupted, spy(decisions_b), CostModel(), start_i=60, end_i=len(bars))
    upto = [d for d in decisions_a if d[0] <= cut]
    assert upto == [d for d in decisions_b if d[0] <= cut]


def test_costs_strictly_reduce_returns():
    bars = regime_series()
    free = run_backtest(bars, momentum_v1, CostModel(0, 0), start_i=60, end_i=len(bars))
    paid = run_backtest(bars, momentum_v1, CostModel(), start_i=60, end_i=len(bars))
    assert free.total_return > paid.total_return


def test_golden_regression_pins():
    """Any drift in engine/strategy/fixture behaviour fails here (Doc 07 §3)."""
    r = run_backtest(regime_series(), momentum_v1, CostModel(), start_i=60, end_i=1200)
    assert r.total_return == pytest.approx(1.0200745813149137)
    assert r.sharpe == pytest.approx(1.8469518012915334)
    assert r.n_trades == 30
    assert r.max_drawdown == pytest.approx(-0.11116306108617846)
