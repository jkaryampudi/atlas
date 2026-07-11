"""Event-driven single-instrument backtester (Phase 3 v1).

No-look-ahead is STRUCTURAL: the strategy callable receives only bars[:i+1]; entry
executes at the NEXT bar's open. Exits: stop (pessimistic at stop price), target,
or time stop. Costs: commission + slippage bps per side.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class OBar:
    open: float; high: float; low: float; close: float; volume: float  # noqa: E702


@dataclass(frozen=True)
class Intent:
    stop: float
    target: float
    time_stop: int  # max holding days


Strategy = Callable[[list[OBar]], Optional[Intent]]


@dataclass(frozen=True)
class Trade:
    entry_i: int; exit_i: int; entry: float; exit: float; reason: str  # noqa: E702

    @property
    def ret(self) -> float:
        return self.exit / self.entry - 1.0


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 5.0
    slippage_bps: float = 5.0

    def buy(self, px: float) -> float:
        return px * (1 + (self.commission_bps + self.slippage_bps) / 10_000)

    def sell(self, px: float) -> float:
        return px * (1 - (self.commission_bps + self.slippage_bps) / 10_000)


@dataclass(frozen=True)
class Result:
    trades: list[Trade]
    equity: list[float]           # daily, starts at 1.0
    total_return: float
    sharpe: float
    max_drawdown: float
    hit_rate: float
    n_trades: int


def run_backtest(bars: list[OBar], strategy: Strategy,
                 costs: CostModel = CostModel(),
                 start_i: int = 0, end_i: int | None = None) -> Result:
    end_i = len(bars) if end_i is None else end_i
    equity = [1.0]
    trades: list[Trade] = []
    pos_entry: float | None = None
    pos_intent: Intent | None = None
    pos_entry_i = -1
    pending: Intent | None = None

    for i in range(max(start_i, 1), end_i):
        b = bars[i]
        day_ret = 0.0
        # 1) execute pending entry at today's open
        if pending is not None and pos_entry is None:
            pos_entry = costs.buy(b.open)
            pos_intent = pending
            pos_entry_i = i
            pending = None
        # 2) manage open position
        if pos_entry is not None and pos_intent is not None:
            exit_px: float | None = None
            reason = ""
            if b.low <= pos_intent.stop:
                exit_px, reason = costs.sell(pos_intent.stop), "stop"
            elif b.high >= pos_intent.target:
                exit_px, reason = costs.sell(pos_intent.target), "target"
            elif i - pos_entry_i >= pos_intent.time_stop:
                exit_px, reason = costs.sell(b.close), "time"
            if exit_px is not None:
                trades.append(Trade(pos_entry_i, i, pos_entry, exit_px, reason))
                day_ret = exit_px / (bars[i - 1].close if reason != "stop" else pos_entry)
                # mark-to-market path: yesterday close -> exit today
                prev_mark = bars[i - 1].close if i - 1 > pos_entry_i else pos_entry
                day_ret = exit_px / prev_mark - 1.0
                pos_entry = pos_intent = None
            else:
                prev_mark = bars[i - 1].close if i - 1 >= pos_entry_i else pos_entry
                day_ret = b.close / prev_mark - 1.0
        # 3) ask strategy (sees ONLY history up to and including today)
        if pos_entry is None and pending is None:
            pending = strategy(bars[: i + 1])
        equity.append(equity[-1] * (1 + day_ret))

    rets = [equity[j] / equity[j - 1] - 1 for j in range(1, len(equity))]
    mu = statistics.fmean(rets) if rets else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mu / sd) * math.sqrt(252) if sd > 0 else 0.0
    peak, mdd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    wins = sum(1 for t in trades if t.ret > 0)
    return Result(trades=trades, equity=equity, total_return=equity[-1] - 1,
                  sharpe=sharpe, max_drawdown=mdd,
                  hit_rate=wins / len(trades) if trades else 0.0,
                  n_trades=len(trades))
