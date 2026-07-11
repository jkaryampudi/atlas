"""Strategy validation gates (ADR-0002): deflated Sharpe on the TRUE trial count,
null-model Monte Carlo (random entries, identical exits + costs), buy-and-hold test.
Approval requires ALL gates (Doc 08 Phase 3)."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import NormalDist

from atlas.dcp.backtest.engine import CostModel, Intent, OBar, Result, Strategy, run_backtest

_ND = NormalDist()
_EULER = 0.5772156649


def deflated_sharpe(sr_annual: float, n_days: int, n_trials: int) -> float:
    """Probability the observed Sharpe exceeds the expected max of n_trials noise
    strategies (simplified Bailey/López de Prado; normal-returns assumption noted)."""
    if n_days < 30:
        return 0.0
    sr_daily = sr_annual / math.sqrt(252)
    if n_trials <= 1:
        e_max = 0.0
    else:
        e_max = math.sqrt(1.0 / n_days) * (
            (1 - _EULER) * _ND.inv_cdf(1 - 1.0 / n_trials)
            + _EULER * _ND.inv_cdf(1 - 1.0 / (n_trials * math.e)))
    z = (sr_daily - e_max) * math.sqrt(n_days - 1)
    return _ND.cdf(z)


def null_distribution(bars: list[OBar], exits: Intent, n_entries: int,
                      costs: CostModel, paths: int, seed: int,
                      start_i: int, end_i: int) -> list[float]:
    """Random-entry strategies with the SAME exit machinery and costs."""
    rng = random.Random(seed)
    out: list[float] = []
    span = list(range(start_i, end_i - exits.time_stop - 2))
    for _ in range(paths):
        entry_days = set(rng.sample(span, min(n_entries, len(span))))

        def null_strat(hist: list[OBar], _e: set[int] = entry_days) -> Intent | None:
            i = len(hist) - 1
            if i in _e:
                c = hist[-1].close
                width_s = 1 - exits.stop        # exits passed as FRACTIONS of price
                width_t = exits.target - 1
                return Intent(stop=c * (1 - width_s), target=c * (1 + width_t),
                              time_stop=exits.time_stop)
            return None

        r = run_backtest(bars, null_strat, costs, start_i=start_i, end_i=end_i)
        out.append(r.total_return)
    return out


def buy_and_hold_return(bars: list[OBar], costs: CostModel,
                        start_i: int, end_i: int) -> float:
    entry = costs.buy(bars[start_i].open)
    exit_ = costs.sell(bars[end_i - 1].close)
    return exit_ / entry - 1.0


@dataclass(frozen=True)
class GateReport:
    strategy_return: float
    bh_return: float
    null_p_value: float          # fraction of null paths >= strategy
    dsr: float
    n_trials: int
    passed: bool
    reasons: list[str]


def null_model_gate(*, bars: list[OBar], strategy: Strategy, result: Result,
                    avg_stop_frac: float, avg_target_frac: float, time_stop: int,
                    costs: CostModel, start_i: int, end_i: int,
                    n_trials: int, paths: int = 200, seed: int = 7,
                    p_max: float = 0.05, dsr_min: float = 0.90) -> GateReport:
    nulls = null_distribution(
        bars, Intent(stop=1 - avg_stop_frac, target=1 + avg_target_frac,
                     time_stop=time_stop),
        n_entries=max(result.n_trades, 1), costs=costs, paths=paths, seed=seed,
        start_i=start_i, end_i=end_i)
    p = sum(1 for x in nulls if x >= result.total_return) / len(nulls)
    bh = buy_and_hold_return(bars, costs, start_i, end_i)
    dsr = deflated_sharpe(result.sharpe, end_i - start_i, n_trials)
    reasons: list[str] = []
    if p > p_max:
        reasons.append(f"null-model: p={p:.3f} > {p_max} (random entries do as well)")
    if result.total_return <= bh:
        reasons.append(f"does not beat buy-and-hold ({result.total_return:.1%} <= {bh:.1%})")
    if dsr < dsr_min:
        reasons.append(f"deflated Sharpe {dsr:.2f} < {dsr_min} at n_trials={n_trials}")
    return GateReport(strategy_return=result.total_return, bh_return=bh,
                      null_p_value=p, dsr=dsr, n_trials=n_trials,
                      passed=not reasons, reasons=reasons)
