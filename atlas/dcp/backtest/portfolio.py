"""Portfolio backtester (strategy R&D round 2): monthly-rebalanced, long-only,
target-weight engine over an aligned multi-symbol daily panel — the portfolio
sibling of the single-instrument engine.py (which stays untouched).

No-look-ahead is STRUCTURAL, exactly as in engine.py: at each rebalance
session t the strategy receives a PanelView clamped at t — its accessors
physically cannot read past t — and the chosen target weights execute at the
NEXT session's open. A rebalance date whose next session falls outside the
window never trades (the portfolio analogue of a signal on the last bar).

Accounting is in weight space (algebraically identical to share counts, and
immune to negative-cash artifacts): equity is marked daily at closes; between
marks, held weights drift with relative prices; at an execution open the
drifted weights are traded to the targets and the CostModel's
commission+slippage bps are charged PER SIDE on the traded notional
(sum(|Δweight|) × equity — each |Δ| is one side of a trade), deducted
multiplicatively from equity. Cash earns nothing. Long-only: a negative target
weight or gross target > 1 is a validation error, so no strategy output can
create shorting or leverage.

Documented resolution: the prescribed "close matrix" carries an aligned OPEN
matrix alongside the closes, because house convention executes at the next
session's open; strategies are only ever shown closes (PanelView exposes no
opens). A `end` keyword was added to the prescribed signature so purged
walk-forward folds can run bounded test windows.

Deterministic, pure, no DB access.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from typing import Callable, Mapping

from atlas.dcp.backtest.engine import CostModel

_GROSS_TOL = 1e-9


@dataclass(frozen=True)
class PricePanel:
    """Aligned daily panel: for every symbol, opens[s][i] / closes[s][i] are the
    prices on dates[i], or None when the symbol has no bar that session (before
    listing / after its series ends). Each symbol's non-None run must be
    CONTIGUOUS — the runner's completeness rule excludes gappy series, and the
    engine's fail-closed arithmetic relies on it."""

    dates: list[date]
    opens: dict[str, list[float | None]]
    closes: dict[str, list[float | None]]

    def __post_init__(self) -> None:
        n = len(self.dates)
        if n == 0:
            raise ValueError("empty panel")
        if any(self.dates[i] >= self.dates[i + 1] for i in range(n - 1)):
            raise ValueError("panel dates must be strictly ascending")
        if set(self.opens) != set(self.closes):
            raise ValueError("opens and closes must cover the same symbols")
        for s in self.closes:
            o, c = self.opens[s], self.closes[s]
            if len(o) != n or len(c) != n:
                raise ValueError(f"{s}: series length != panel length")
            mask = [x is not None for x in c]
            if mask != [x is not None for x in o]:
                raise ValueError(f"{s}: open/close availability disagree")
            lit = [i for i, m in enumerate(mask) if m]
            if not lit:
                raise ValueError(f"{s}: series has no data at all")
            if lit[-1] - lit[0] + 1 != len(lit):
                raise ValueError(f"{s}: series has holes — exclude it upstream")

    def index_at(self, day: date) -> int:
        """First index i with dates[i] >= day (bisect; raises past the end)."""
        i = bisect_left(self.dates, day)
        if i >= len(self.dates):
            raise ValueError(f"{day} is after the panel ends ({self.dates[-1]})")
        return i


class PanelView:
    """Strategy-facing read-only window of a panel, clamped at index t.
    Structural no-look-ahead: close(s, i) raises for i > t; only closes are
    exposed (decisions are made after the close, executed at the next open)."""

    __slots__ = ("_panel", "_t")

    def __init__(self, panel: PricePanel, t: int) -> None:
        if not 0 <= t < len(panel.dates):
            raise ValueError(f"view index {t} outside panel")
        self._panel = panel
        self._t = t

    @property
    def t(self) -> int:
        return self._t

    @property
    def n(self) -> int:
        """Number of visible sessions (t + 1)."""
        return self._t + 1

    @property
    def today(self) -> date:
        return self._panel.dates[self._t]

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._panel.closes))

    def close(self, symbol: str, i: int) -> float | None:
        """Close of `symbol` on session i. None before the panel begins (i < 0)
        or when the symbol has no bar; ValueError past the clamp (look-ahead)."""
        if i > self._t:
            raise ValueError(f"look-ahead: session {i} > view clamp {self._t}")
        if i < 0:
            return None
        return self._panel.closes[symbol][i]


PortfolioStrategy = Callable[[PanelView], Mapping[str, float]]
"""At rebalance session t the strategy sees ONLY data <= t (the view) and
returns target weights (symbol -> fraction of equity); the remainder is cash."""


@dataclass(frozen=True)
class PortfolioResult:
    total_return: float
    sharpe: float                # daily returns, annualised sqrt(252)
    max_drawdown: float
    avg_turnover: float          # mean over rebalances of sum(|Δweight|), both sides
    n_rebalances: int
    equity_curve: list[float]    # daily closes, starts at 1.0 on the start session
    dates: list[date]            # aligned to equity_curve (reporting aid)


def month_end_indices(dates: list[date], start_i: int, end_i: int) -> list[int]:
    """Rebalance schedule: the last session of each calendar month, restricted
    to [start_i, end_i - 2] so the next-session execution stays inside the
    window (t + 1 is therefore always a valid index). Month ends are judged on
    the panel's own session sequence, so the window's final session — usually
    mid-month — is never mistaken for a month end."""
    return [t for t in range(start_i, end_i - 1)
            if dates[t].month != dates[t + 1].month]


def _validated_targets(raw: Mapping[str, float], panel: PricePanel,
                       t: int) -> dict[str, float]:
    """Long-only, no leverage, tradable at decision time — else ValueError."""
    out: dict[str, float] = {}
    for s, w in raw.items():
        if s not in panel.closes:
            raise ValueError(f"target for unknown symbol {s!r}")
        if not math.isfinite(w) or w < 0:
            raise ValueError(f"{s}: weight {w!r} violates long-only/no-leverage")
        if panel.closes[s][t] is None:
            raise ValueError(f"{s}: targeted without a price at decision "
                             f"session {panel.dates[t]}")
        if w > 0:
            out[s] = float(w)
    gross = sum(out.values())
    if gross > 1.0 + _GROSS_TOL:
        raise ValueError(f"gross target {gross:.6f} > 1 (no leverage)")
    return out


def _price(series: list[float | None], i: int, symbol: str, when: date) -> float:
    px = series[i]
    if px is None:
        raise ValueError(f"{symbol}: missing price while held at {when} — "
                         "panel completeness rule violated")
    return px


def _drift(equity: float, weights: dict[str, float], panel: PricePanel, i: int,
           *, phase: str) -> tuple[float, dict[str, float]]:
    """Move the mark: 'close' = close[i-1] -> close[i]; 'to_open' =
    close[i-1] -> open[i]; 'open_close' = open[i] -> close[i]. Held weights
    drift with relative prices; cash contributes zero return."""
    if not weights:
        return equity, weights
    growth: dict[str, float] = {}
    when = panel.dates[i]
    for s in weights:
        if phase == "close":
            p0 = _price(panel.closes[s], i - 1, s, when)
            p1 = _price(panel.closes[s], i, s, when)
        elif phase == "to_open":
            p0 = _price(panel.closes[s], i - 1, s, when)
            p1 = _price(panel.opens[s], i, s, when)
        else:  # "open_close"
            p0 = _price(panel.opens[s], i, s, when)
            p1 = _price(panel.closes[s], i, s, when)
        growth[s] = p1 / p0
    held = sum(weights.values())
    g = sum(w * growth[s] for s, w in weights.items()) + (1.0 - held)
    return equity * g, {s: w * growth[s] / g for s, w in weights.items()}


def turnover(current: Mapping[str, float], target: Mapping[str, float]) -> float:
    """Sum of |Δweight| over the union of names: each unit is ONE side of a
    trade (a buy or a sell), each charged the per-side cost rate."""
    return sum(abs(target.get(s, 0.0) - current.get(s, 0.0))
               for s in set(current) | set(target))


def run_portfolio_backtest(prices: PricePanel, strategy: PortfolioStrategy,
                           costs: CostModel = CostModel(), *,
                           start: date, rebalance: str = "monthly",
                           end: date | None = None) -> PortfolioResult:
    """Monthly rebalance at t (strategy sees only data <= t), execution at the
    next session's open, per-side bps on turnover, daily close marks."""
    if rebalance != "monthly":
        raise ValueError(f"unsupported rebalance schedule {rebalance!r}")
    dates = prices.dates
    start_i = prices.index_at(start)
    end_i = len(dates) if end is None else bisect_right(dates, end)
    if end_i - start_i < 2:
        raise ValueError("window too short: need at least two sessions")
    side_rate = (costs.commission_bps + costs.slippage_bps) / 10_000
    reb = set(month_end_indices(dates, start_i, end_i))

    equity = 1.0
    curve = [equity]
    weights: dict[str, float] = {}
    pending: dict[str, float] | None = None
    turnovers: list[float] = []

    if start_i in reb:
        pending = _validated_targets(strategy(PanelView(prices, start_i)),
                                     prices, start_i)
    for i in range(start_i + 1, end_i):
        if pending is not None:
            # execute at today's open: drift to open, trade, pay per-side costs
            equity, weights = _drift(equity, weights, prices, i, phase="to_open")
            t_over = turnover(weights, pending)
            equity *= 1.0 - t_over * side_rate
            weights = dict(pending)
            turnovers.append(t_over)
            pending = None
            equity, weights = _drift(equity, weights, prices, i, phase="open_close")
        else:
            equity, weights = _drift(equity, weights, prices, i, phase="close")
        curve.append(equity)
        if i in reb:
            pending = _validated_targets(strategy(PanelView(prices, i)), prices, i)

    rets = [curve[j] / curve[j - 1] - 1 for j in range(1, len(curve))]
    mu = statistics.fmean(rets) if rets else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mu / sd) * math.sqrt(252) if sd > 0 else 0.0
    peak, mdd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    return PortfolioResult(
        total_return=curve[-1] - 1.0, sharpe=sharpe, max_drawdown=mdd,
        avg_turnover=statistics.fmean(turnovers) if turnovers else 0.0,
        n_rebalances=len(turnovers), equity_curve=curve,
        dates=list(dates[start_i:end_i]))
