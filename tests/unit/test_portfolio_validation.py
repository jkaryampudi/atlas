"""Portfolio validation gates: thresholds are IMPORTED from validation.py
(pinned here so restating/loosening them fails a test), the monkey null is
seed-deterministic and draws from the strategy's OWN eligible set (proved by
the n_pick >= |eligible| equivalence with the equal-weight benchmark), the
SPY comparison is binding while equal-weight is informational, and the purged
walk-forward over the daily timeline stays leakage-free."""
import inspect
import math
import random
from datetime import date, timedelta
from statistics import fmean

import pytest

from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.backtest.portfolio import PortfolioResult, PricePanel, run_portfolio_backtest
from atlas.dcp.backtest.portfolio_validation import (
    DSR_MIN,
    P_MAX,
    buy_and_hold_strategy,
    equal_weight_eligible,
    portfolio_gate,
    portfolio_null_distribution,
    portfolio_walk_forward,
)
from atlas.dcp.backtest.validation import null_model_gate
from atlas.dcp.signals.xsmom.v1 import SEASONING

COSTS = CostModel()


def test_thresholds_are_imported_from_validation_never_restated():
    """Single source of truth: the committed defaults of null_model_gate.
    If someone hardcodes different portfolio thresholds, this fails."""
    params = inspect.signature(null_model_gate).parameters
    assert P_MAX == params["p_max"].default == 0.05
    assert DSR_MIN == params["dsr_min"].default == 0.90


def weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _rw_panel(n: int = 300, syms: int = 8, seed: int = 5) -> PricePanel:
    rng = random.Random(seed)
    dates = weekdays(date(2023, 1, 2), n)
    opens: dict[str, list[float | None]] = {}
    closes: dict[str, list[float | None]] = {}
    for k in range(syms):
        px = 100.0
        o: list[float | None] = []
        c: list[float | None] = []
        for _ in range(n):
            o.append(px)
            px *= math.exp(0.012 * rng.gauss(0, 1))
            c.append(px)
        opens[f"R{k}"], closes[f"R{k}"] = o, c
    return PricePanel(dates=dates, opens=opens, closes=closes)


def test_null_distribution_is_seed_deterministic():
    panel = _rw_panel()
    start = panel.dates[SEASONING]
    a = portfolio_null_distribution(panel, costs=COSTS, start=start,
                                    n_pick=3, paths=12, seed=3)
    b = portfolio_null_distribution(panel, costs=COSTS, start=start,
                                    n_pick=3, paths=12, seed=3)
    c = portfolio_null_distribution(panel, costs=COSTS, start=start,
                                    n_pick=3, paths=12, seed=4)
    assert a == b
    assert a != c
    assert len(a) == 12 and len(set(a)) > 1     # picks actually vary


def test_monkey_draws_from_the_same_eligible_set():
    """With n_pick >= |eligible| every monkey holds ALL eligible names equal
    weight — exactly the equal-weight benchmark. Any drift between the
    monkey's universe and the strategy's eligible set would break this."""
    panel = _rw_panel()
    start = panel.dates[SEASONING]
    nulls = portfolio_null_distribution(panel, costs=COSTS, start=start,
                                        n_pick=99, paths=5, seed=7)
    ew = run_portfolio_backtest(panel, equal_weight_eligible, COSTS, start=start)
    assert nulls == pytest.approx([ew.total_return] * 5)


def _res(total: float, sharpe: float = 2.5, n_days: int = 500) -> PortfolioResult:
    curve = [1.0] * n_days + [1.0 + total]
    return PortfolioResult(total_return=total, sharpe=sharpe, max_drawdown=0.0,
                           avg_turnover=0.5, n_rebalances=3,
                           equity_curve=curve, dates=[])


def test_gate_passes_only_on_all_three_and_spy_is_binding():
    nulls = [-0.1] * 99 + [0.1]
    g = portfolio_gate(result=_res(0.5), null_returns=nulls,
                       spy=_res(0.3), ew=_res(0.6), n_trials=1)
    # beats SPY (binding) but NOT equal-weight (informational, still reported)
    assert g.passed and g.reasons == []
    assert g.null_p_value == pytest.approx(0.0)
    assert g.spy_bh_return == pytest.approx(0.3)
    assert g.ew_return == pytest.approx(0.6)


def test_gate_fails_on_spy_even_when_null_and_dsr_pass():
    g = portfolio_gate(result=_res(0.5), null_returns=[-0.1] * 100,
                       spy=_res(0.6), ew=_res(0.1), n_trials=1)
    assert not g.passed
    assert g.reasons == ["does not beat SPY buy-and-hold (50.0% <= 60.0%)"]


def test_gate_fails_on_null_p_value_ties_count_against():
    g = portfolio_gate(result=_res(0.5), null_returns=[0.5] * 100,
                       spy=_res(0.3), ew=_res(0.3), n_trials=1)
    assert not g.passed
    assert g.null_p_value == pytest.approx(1.0)     # >= is a tie, counts
    assert any(r.startswith("null-model: p=1.000 > 0.05") for r in g.reasons)


def test_gate_fails_on_deflated_sharpe_at_true_trial_count():
    g = portfolio_gate(result=_res(0.5, sharpe=0.2), null_returns=[-0.1] * 100,
                       spy=_res(0.3), ew=_res(0.3), n_trials=50)
    assert not g.passed
    assert any("deflated Sharpe" in r and "n_trials=50" in r for r in g.reasons)


def test_buy_and_hold_pays_costs_exactly_once():
    """Sole holding, no cash: the drifted weight equals the target at every
    later rebalance, so turnover — and cost — hits only the first execution."""
    panel = _rw_panel()
    start = panel.dates[0]
    r = run_portfolio_backtest(panel, buy_and_hold_strategy("R0"), COSTS,
                               start=start)
    assert r.n_rebalances >= 2
    assert r.avg_turnover == pytest.approx(1.0 / r.n_rebalances)


def test_walk_forward_folds_run_and_aggregate():
    panel = _rw_panel(n=340)
    wf = portfolio_walk_forward(panel, equal_weight_eligible, k=2, horizon=40,
                                embargo=10, warmup=SEASONING, costs=COSTS)
    assert len(wf.fold_results) == 2
    rets = [r.total_return for r in wf.fold_results]
    assert wf.mean_return == pytest.approx(fmean(rets))
    assert wf.worst_fold_return == pytest.approx(min(rets))
    assert wf.positive_folds == sum(1 for x in rets if x > 0)
    assert wf.mean_sharpe == pytest.approx(
        fmean(r.sharpe for r in wf.fold_results))
