"""Sealed FX sandbox engine (ADR-0008): single-pair daily long/short backtester.

Position is in {-1, 0, +1}. No-look-ahead is STRUCTURAL (CLAUDE.md invariant 8,
same construction as dcp/backtest/engine.py): the position held during bar t's
session is ``strategy(bars[:t])`` — decided on the close of t-1 with only
history through t-1 visible, executed at the open of t.

Honest FX economics (ADR-0008 §4) — both constants are conservative,
documented placeholders, recalibratable ONLY as an ADR-0003 Tier-1
measured-cost update (never in response to a strategy's own P&L):

- ``SPREAD_PER_SIDE`` = 0.00008, charged in return space on EVERY position-
  change leg: entering or exiting costs one leg, a +1 -> -1 flip costs two.
  A full round trip therefore pays 2 x 0.8 pips = 1.6 pips — deliberately at
  or above the top of typical all-in retail EUR/USD spread costs (~0.6-1.0
  pips round trip); costs may only ever be biased AGAINST the candidate.
- ``SWAP_PER_NIGHT`` = 0.00003, charged per bar-transition night a nonzero
  position is held, EITHER direction — a symmetric worst-of approximation
  (real swaps are signed and asymmetric, and weekends roll triple; daily bars
  see one Friday->Monday transition, counted as one night).

Daily mark-to-market in return space from 1.0. For the day-t session with
overnight position ``prev`` and session position ``pos``:

    r_t = prev * (open_t / close_{t-1} - 1)          # overnight gap
        + pos  * (close_t / open_t   - 1)            # session move
        - |pos - prev| * SPREAD_PER_SIDE             # rebalance legs at open
        - (SWAP_PER_NIGHT if prev != 0 else 0)       # the night just crossed

Any position still open after the final bar is force-liquidated at that
bar's close and pays its closing leg(s) — total_return is fully after-cost,
as ADR-0008 §5's benchmark-zero comparison requires.

The benchmark is zero (ADR-0008 §5): there is nothing to hold in FX, so no
buy-and-hold leg exists here. No profit target exists anywhere in this module.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Callable

SPREAD_PER_SIDE = 0.00008   # 0.8 pips per leg (see module docstring)
SWAP_PER_NIGHT = 0.00003    # per night held, either direction (see module docstring)

_VALID_POSITIONS = (-1, 0, 1)


@dataclass(frozen=True)
class FxBar:
    """Daily EUR/USD OHLC. Deliberately NO volume field: EODHD FOREX volume is
    untrustworthy (frequently 0) and must not exist to be leaned on."""
    bar_date: date
    open: float
    high: float
    low: float
    close: float


FxStrategy = Callable[[list[FxBar]], int]
"""Pure function over the visible history; returns the desired position
in {-1, 0, +1} for the NEXT session (executed at its open)."""


@dataclass(frozen=True)
class FxResult:
    total_return: float          # after ALL costs, including final liquidation
    sharpe: float                # annualized, sqrt(252), population stdev
    max_drawdown: float
    n_trades: int                # entries into a nonzero position (flips count)
    exposure_long: float         # share of session days held +1
    exposure_short: float        # share of session days held -1
    exposure_flat: float         # share of session days held 0
    equity: list[float]          # daily, starts at 1.0
    positions: list[int]         # session position per evaluated day


def run_fx_positions(bars: list[FxBar], positions: list[int],
                     start_i: int) -> FxResult:
    """Evaluate an explicit position sequence: ``positions[j]`` is held during
    the session of ``bars[start_i + j]``. This is THE cost/accounting code
    path — strategy runs and null-model paths both come through here, so the
    null model pays identical spread and swap (ADR-0008 §4/§5)."""
    if start_i < 1:
        raise ValueError("start_i must be >= 1 (day accounting needs close of t-1)")
    if start_i + len(positions) > len(bars):
        raise ValueError("position sequence runs past the end of the bar series")
    rets: list[float] = []
    prev = 0
    n_trades = 0
    for j, pos in enumerate(positions):
        if pos not in _VALID_POSITIONS:
            raise ValueError(f"position must be -1, 0 or +1, got {pos!r}")
        b = bars[start_i + j]
        prev_close = bars[start_i + j - 1].close
        r = (prev * (b.open / prev_close - 1.0)
             + pos * (b.close / b.open - 1.0)
             - abs(pos - prev) * SPREAD_PER_SIDE
             - (SWAP_PER_NIGHT if prev != 0 else 0.0))
        if pos != prev and pos != 0:
            n_trades += 1
        rets.append(r)
        prev = pos
    if prev != 0 and rets:
        # force-liquidate at the final close: fold the closing leg into the
        # last day's return so total_return is fully after-cost
        rets[-1] = (1.0 + rets[-1]) * (1.0 - SPREAD_PER_SIDE) - 1.0
    equity = [1.0]
    for r in rets:
        equity.append(equity[-1] * (1.0 + r))
    mu = statistics.fmean(rets) if rets else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mu / sd) * math.sqrt(252) if sd > 0 else 0.0
    peak, mdd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    n = len(positions)
    return FxResult(
        total_return=equity[-1] - 1.0, sharpe=sharpe, max_drawdown=mdd,
        n_trades=n_trades,
        exposure_long=sum(1 for p in positions if p > 0) / n if n else 0.0,
        exposure_short=sum(1 for p in positions if p < 0) / n if n else 0.0,
        exposure_flat=sum(1 for p in positions if p == 0) / n if n else 0.0,
        equity=equity, positions=list(positions))


def run_fx_backtest(bars: list[FxBar], strategy: FxStrategy, start_i: int,
                    end_i: int | None = None) -> FxResult:
    """Run a strategy over ``bars[start_i:end_i]`` sessions. The position for
    the day-t session is ``strategy(bars[:t])`` — the strategy NEVER sees bar
    t or anything after it when deciding the position held through t."""
    end_i = len(bars) if end_i is None else end_i
    if not 1 <= start_i < end_i <= len(bars):
        raise ValueError(f"bad window [{start_i}, {end_i}) for {len(bars)} bars")
    positions = [int(strategy(bars[:t])) for t in range(start_i, end_i)]
    return run_fx_positions(bars, positions, start_i)
