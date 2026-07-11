"""Purged + embargoed walk-forward evaluation (ADR-0002 #3, López de Prado).

Why purging: a decision at day t with holding horizon h carries label information
from [t, t+h]. If t sits just before a test window, its label leaks test data into
training. Purge removes train days whose label window overlaps the test window;
embargo additionally excludes days immediately AFTER the test window (serial
correlation leaks backwards too).
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Callable

from atlas.dcp.backtest.engine import CostModel, OBar, Result, Strategy, run_backtest


@dataclass(frozen=True)
class Fold:
    train_days: list[int]        # purged + embargoed
    test_start: int
    test_end: int                # exclusive


def purged_folds(n_days: int, *, k: int, horizon: int, embargo: int,
                 warmup: int = 0) -> list[Fold]:
    if k < 2:
        raise ValueError("need at least 2 folds")
    span = n_days - warmup
    size = span // k
    folds: list[Fold] = []
    for f in range(k):
        a = warmup + f * size
        b = warmup + (f + 1) * size if f < k - 1 else n_days
        train = [t for t in range(warmup, n_days)
                 if not (a - horizon <= t < b + embargo)]
        folds.append(Fold(train_days=train, test_start=a, test_end=b))
    return folds


def leakage_free(fold: Fold, *, horizon: int, embargo: int) -> bool:
    a, b = fold.test_start, fold.test_end
    return all(not (a - horizon <= t < b + embargo) for t in fold.train_days)


@dataclass(frozen=True)
class WalkForwardResult:
    fold_results: list[Result]
    mean_return: float
    mean_sharpe: float
    worst_fold_return: float
    positive_folds: int


StrategyFactory = Callable[[list[OBar], list[int]], Strategy]
"""Receives (all bars, train day indices) and returns a strategy for the test fold.
Unfitted strategies (momentum v1) ignore both; fitted ones may ONLY read train days."""


def walk_forward(bars: list[OBar], factory: StrategyFactory, *,
                 k: int, horizon: int, embargo: int, warmup: int,
                 costs: CostModel = CostModel()) -> WalkForwardResult:
    results: list[Result] = []
    for fold in purged_folds(len(bars), k=k, horizon=horizon, embargo=embargo,
                             warmup=warmup):
        assert leakage_free(fold, horizon=horizon, embargo=embargo)
        strat = factory(bars, fold.train_days)
        results.append(run_backtest(bars, strat, costs,
                                    start_i=fold.test_start, end_i=fold.test_end))
    rets = [r.total_return for r in results]
    return WalkForwardResult(
        fold_results=results,
        mean_return=fmean(rets),
        mean_sharpe=fmean(r.sharpe for r in results),
        worst_fold_return=min(rets),
        positive_folds=sum(1 for x in rets if x > 0))
