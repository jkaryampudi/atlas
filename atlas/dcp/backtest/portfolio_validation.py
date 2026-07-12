"""Portfolio-level validation gates — the same three ADR-0002 gates as
validation.py (null-model Monte Carlo, deflated Sharpe on the TRUE trial
count, beat-buy-and-hold), adapted to the portfolio engine.

THRESHOLDS ARE IMPORTED, NEVER RESTATED: P_MAX and DSR_MIN are read from the
signature defaults of validation.null_model_gate — the single committed source
of truth. Restating the numbers here would create a second place to weaken
them; tests pin the equality.

Null model (the monkey control, ADR-0002 #2): at each rebalance, draw TOP_N
names uniformly from the SAME eligible set the strategy ranks over,
equal-weighted, through the IDENTICAL engine — same costs, same next-open
execution, same turnover mechanics. If ranking by 12-1 momentum cannot beat
dart-throwing over the same names, the ranking carries no information.

Benchmarks are computed in the SAME engine (identical execution and costs):
(a) SPY buy-and-hold — the fund's actual alternative and therefore the
BINDING beat-buy-and-hold comparison; (b) equal-weight over ALL eligible
names, rebalanced monthly — reported alongside so selection skill is
separated from the equal-weight/small-cap tilt, but NOT binding.

Purged walk-forward mapping (documented honestly): folds are cut on the DAILY
session timeline with real_run's K_FOLDS/HORIZON/EMBARGO constants, exactly as
the single-series harness cuts them. A decision at month-end t is exposed
until the next rebalance executes (~21 sessions + 1 execution day), so
HORIZON=40 sessions strictly dominates the true label horizon and the purge is
conservative. xsmom v1 is unfitted (no trained parameters), so folds serve as
sub-period robustness checks — the same role they play for momentum v1 in
real_run; each fold's test window simply runs the portfolio engine over
[test_start, test_end).
"""
from __future__ import annotations

import inspect
import random
from dataclasses import dataclass
from datetime import date
from statistics import fmean
from typing import Final

from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.backtest.portfolio import (
    PanelView,
    PortfolioResult,
    PortfolioStrategy,
    PricePanel,
    run_portfolio_backtest,
)
from atlas.dcp.backtest.validation import deflated_sharpe, null_model_gate
from atlas.dcp.backtest.walkforward import leakage_free, purged_folds
from atlas.dcp.signals.xsmom.v1 import eligible_symbols

_GATE_PARAMS = inspect.signature(null_model_gate).parameters
P_MAX: Final[float] = float(_GATE_PARAMS["p_max"].default)
DSR_MIN: Final[float] = float(_GATE_PARAMS["dsr_min"].default)


def portfolio_null_distribution(panel: PricePanel, *, costs: CostModel,
                                start: date, n_pick: int, paths: int,
                                seed: int) -> list[float]:
    """Seeded monkey portfolios: at each rebalance, n_pick names drawn
    uniformly (without replacement) from the SAME eligible set, equal weight,
    identical engine mechanics. One rng drives all paths sequentially — the
    validation.py convention — so (seed, paths) fully determines the output.

    The eligible set is cached per rebalance index: it is a pure property of
    the panel (strategy-independent), so every path — and the strategy run —
    faces the identical universe by construction (pinned by test)."""
    rng = random.Random(seed)
    cache: dict[int, list[str]] = {}

    def monkey(view: PanelView) -> dict[str, float]:
        elig = cache.get(view.t)
        if elig is None:
            elig = eligible_symbols(view)
            cache[view.t] = elig
        if not elig:
            return {}
        pick = rng.sample(elig, min(n_pick, len(elig)))
        w = 1.0 / len(pick)
        return {s: w for s in pick}

    out: list[float] = []
    for _ in range(paths):
        r = run_portfolio_backtest(panel, monkey, costs, start=start)
        out.append(r.total_return)
    return out


def equal_weight_eligible(view: PanelView) -> dict[str, float]:
    """Benchmark (b): equal weight over ALL eligible names, monthly."""
    elig = eligible_symbols(view)
    if not elig:
        return {}
    w = 1.0 / len(elig)
    return {s: w for s in elig}


def buy_and_hold_strategy(symbol: str) -> PortfolioStrategy:
    """Benchmark (a): 100% one symbol at every rebalance. After the first
    execution the drifted weight already equals the target (sole holding, no
    cash), so turnover — and therefore cost — is paid exactly once."""
    def strat(view: PanelView) -> dict[str, float]:
        return {symbol: 1.0}
    return strat


@dataclass(frozen=True)
class PortfolioGateReport:
    strategy_return: float
    spy_bh_return: float         # binding beat-buy-and-hold comparison
    ew_return: float             # informational: equal-weight all eligible
    null_p_value: float          # fraction of monkey paths >= strategy
    dsr: float
    n_trials: int
    passed: bool
    reasons: list[str]


def portfolio_gate(*, result: PortfolioResult, null_returns: list[float],
                   spy: PortfolioResult, ew: PortfolioResult,
                   n_trials: int) -> PortfolioGateReport:
    """Verdict under the UNMODIFIED thresholds imported above. The binding
    buy-and-hold comparison is SPY (the fund's actual alternative); the
    equal-weight benchmark is carried in the report, not the verdict."""
    p = sum(1 for x in null_returns if x >= result.total_return) / len(null_returns)
    dsr = deflated_sharpe(result.sharpe, len(result.equity_curve) - 1, n_trials)
    reasons: list[str] = []
    if p > P_MAX:
        reasons.append(f"null-model: p={p:.3f} > {P_MAX} "
                       "(random same-universe portfolios do as well)")
    if result.total_return <= spy.total_return:
        reasons.append(f"does not beat SPY buy-and-hold "
                       f"({result.total_return:.1%} <= {spy.total_return:.1%})")
    if dsr < DSR_MIN:
        reasons.append(f"deflated Sharpe {dsr:.2f} < {DSR_MIN} "
                       f"at n_trials={n_trials}")
    return PortfolioGateReport(
        strategy_return=result.total_return, spy_bh_return=spy.total_return,
        ew_return=ew.total_return, null_p_value=p, dsr=dsr, n_trials=n_trials,
        passed=not reasons, reasons=reasons)


@dataclass(frozen=True)
class PortfolioWalkForwardResult:
    fold_results: list[PortfolioResult]
    mean_return: float
    mean_sharpe: float
    worst_fold_return: float
    positive_folds: int


def portfolio_walk_forward(panel: PricePanel, strategy: PortfolioStrategy, *,
                           k: int, horizon: int, embargo: int, warmup: int,
                           costs: CostModel) -> PortfolioWalkForwardResult:
    """Purged + embargoed folds over the daily session timeline (constants
    from real_run; leakage_free re-asserted per fold, as in walkforward.py)."""
    results: list[PortfolioResult] = []
    for fold in purged_folds(len(panel.dates), k=k, horizon=horizon,
                             embargo=embargo, warmup=warmup):
        assert leakage_free(fold, horizon=horizon, embargo=embargo)
        results.append(run_portfolio_backtest(
            panel, strategy, costs,
            start=panel.dates[fold.test_start],
            end=panel.dates[fold.test_end - 1]))
    rets = [r.total_return for r in results]
    return PortfolioWalkForwardResult(
        fold_results=results,
        mean_return=fmean(rets),
        mean_sharpe=fmean(r.sharpe for r in results),
        worst_fold_return=min(rets),
        positive_folds=sum(1 for x in rets if x > 0))
