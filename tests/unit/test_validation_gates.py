import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from fixtures.synthetic import random_walk, regime_series  # noqa: E402

from atlas.dcp.backtest.engine import CostModel, Intent, run_backtest  # noqa: E402
from atlas.dcp.backtest.validation import deflated_sharpe, null_model_gate  # noqa: E402
from atlas.dcp.signals.momentum.v1 import momentum_v1  # noqa: E402

COSTS = CostModel()


def test_gate_passes_genuine_edge():
    bars = regime_series()
    r = run_backtest(bars, momentum_v1, COSTS, start_i=60, end_i=len(bars))
    g = null_model_gate(bars=bars, strategy=momentum_v1, result=r,
                        avg_stop_frac=0.035, avg_target_frac=0.07,
                        time_stop=40, costs=COSTS,
                        start_i=60, end_i=len(bars), n_trials=1, paths=200)
    assert g.passed and g.null_p_value <= 0.05 and g.strategy_return > g.bh_return


def test_gate_rejects_momentum_on_random_walk():
    rw = random_walk()
    r = run_backtest(rw, momentum_v1, COSTS, start_i=60, end_i=len(rw))
    g = null_model_gate(bars=rw, strategy=momentum_v1, result=r,
                        avg_stop_frac=0.035, avg_target_frac=0.07, time_stop=40,
                        costs=COSTS, start_i=60, end_i=len(rw), n_trials=1, paths=200)
    assert not g.passed


def _canary(k: int, r: int):
    def strat(hist):
        i = len(hist) - 1
        if i % k == r:
            c = hist[-1].close
            return Intent(stop=c * 0.965, target=c * 1.07, time_stop=40)
        return None
    return strat


def test_overfit_canary_is_rejected():
    """Phase 3 exit criterion: mine 68 junk rules in-sample on a random walk,
    take the best-looking one, and the gate MUST kill it out-of-sample."""
    rw = random_walk()
    best, trials = None, 0
    for k in range(5, 13):
        for r in range(k):
            trials += 1
            res = run_backtest(rw, _canary(k, r), COSTS, start_i=60, end_i=800)
            if best is None or res.total_return > best[2]:
                best = (k, r, res.total_return)
    k, r, is_ret = best
    assert is_ret > 0.30                       # the trap looks attractive in-sample
    oos = run_backtest(rw, _canary(k, r), COSTS, start_i=800, end_i=len(rw))
    g = null_model_gate(bars=rw, strategy=_canary(k, r), result=oos,
                        avg_stop_frac=0.035, avg_target_frac=0.07, time_stop=40,
                        costs=COSTS, start_i=800, end_i=len(rw),
                        n_trials=trials, paths=200)
    assert not g.passed
    assert g.dsr < 0.1                         # true trial count destroys the Sharpe


def test_deflated_sharpe_monotone_in_trials():
    assert deflated_sharpe(1.85, 1140, 1) > deflated_sharpe(1.85, 1140, 68) > \
           deflated_sharpe(1.85, 1140, 500)
