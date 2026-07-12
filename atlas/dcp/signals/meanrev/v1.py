"""Mean-reversion v1: short-horizon dip buying — enter long when RSI(2) < 10
AND close > SMA(200); exit when RSI(2) reaches 70 or after 10 bars.

Textbook parameters, NO parameter search: this is the classic Connors long-only
RSI(2) variant — Connors & Alvarez, "Short Term Trading Strategies That Work"
(2008): buy oversold (2-period RSI below 10) only above the 200-day moving
average, exit into strength (RSI(2) above 70), no protective stop (Connors
finds stops degrade this system). Long-only per the Atlas mandate.

Engine mapping (exits are frozen at entry — structural, see engine.py): the
"RSI(2) >= 70" exit is posted as a price target — the exact close that lifts
next-bar Wilder RSI(2) to 70 given the signal-bar average gain/loss, derived
from: RSI' >= X  <=>  (100-X)*ag' >= X*al', with ag' = (ag*(N-1) + g)/N and
al' = al*(N-1)/N on an up-move g, giving g >= (N-1)*(X*al - (100-X)*ag)/(100-X).
The threshold is exact for the first post-signal bar and an approximation
afterwards (the true RSI keeps evolving; the engine cannot re-ask an open
position). Hard exit after MAX_HOLD=10 bars via the engine time stop; stop=0.0
is never hit (no protective stop, per the source).
Same code path serves backtest and production (ADR-0002 #4).
"""
from __future__ import annotations

from atlas.dcp.backtest.engine import Intent, OBar
from atlas.dcp.indicators.core import rsi, sma, wilder_avg_gain_loss

RSI_PERIOD = 2
ENTRY_RSI, EXIT_RSI = 10.0, 70.0
SMA_FILTER = 200
MAX_HOLD = 10
SPEC: dict[str, object] = {"family": "meanrev", "name": "connors_rsi2",
    "version": "1.0.0", "rsi_period": RSI_PERIOD, "entry_rsi": ENTRY_RSI,
    "exit_rsi": EXIT_RSI, "sma_filter": SMA_FILTER, "max_hold": MAX_HOLD,
    "provenance": "textbook (Connors & Alvarez 2008); no search"}


def _rsi_exit_target(close: float, ag: float, al: float) -> float:
    """Price whose up-move lifts next-bar Wilder RSI(RSI_PERIOD) to EXIT_RSI."""
    gain_needed = ((RSI_PERIOD - 1)
                   * (EXIT_RSI * al - (100.0 - EXIT_RSI) * ag)
                   / (100.0 - EXIT_RSI))
    return close + max(gain_needed, 0.0)


def meanrev_v1(bars: list[OBar]) -> Intent | None:
    if len(bars) < SMA_FILTER:
        return None
    closes = [b.close for b in bars]
    s = sma(closes, SMA_FILTER)[-1]
    r = rsi(closes, RSI_PERIOD)[-1]
    pair = wilder_avg_gain_loss(closes, RSI_PERIOD)[-1]
    if s is None or r is None or pair is None:
        return None
    c = closes[-1]
    if c > s and r < ENTRY_RSI:
        return Intent(stop=0.0, target=_rsi_exit_target(c, *pair),
                      time_stop=MAX_HOLD)
    return None
