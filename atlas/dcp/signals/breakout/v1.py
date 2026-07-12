"""Breakout v1: Donchian channel breakout — enter long when the close exceeds
the highest high of the previous 55 bars; exit when price breaks the lowest low
of the previous 20 bars.

Textbook parameters, NO parameter search: these are the classic Turtle System 2
values (Dennis/Eckhardt rules; Faith, "Way of the Turtle", 2007; Donchian's
channel method) — 55-day breakout entry, 20-day-low trailing exit. Long-only
per the Atlas mandate (the Turtles' short side is out of scope).

Engine mapping (exits are frozen at entry — structural, see engine.py): the
in/out channel state is replayed from bar 0 as a pure function of bars[:i+1]
(entry: close > max high of the prior 55 bars; exit: low < min low of the prior
20 bars) — no stored state, so no-look-ahead stays structural. The 20-bar-low
exit is posted as the stop at (re-)entry — min low of the last EXIT_CHANNEL
bars — and refreshed every REEVAL=20 bars (the exit-channel length, not a tuned
value) via the engine time stop, approximating the Turtle trailing exit at
monthly granularity; each refresh pays a full round trip of costs, a
conservative (cost-adding) artifact of the mapping.
Same code path serves backtest and production (ADR-0002 #4).
"""
from __future__ import annotations

import math
from collections import deque

from atlas.dcp.backtest.engine import Intent, OBar

ENTRY_CHANNEL, EXIT_CHANNEL = 55, 20
REEVAL = 20
SPEC: dict[str, object] = {"family": "breakout", "name": "donchian_55_20",
    "version": "1.0.0", "entry_channel": ENTRY_CHANNEL,
    "exit_channel": EXIT_CHANNEL, "reeval": REEVAL,
    "provenance": "textbook (Turtle System 2, Faith 2007); no search"}


def _replay_state(highs: list[float], lows: list[float],
                  closes: list[float]) -> bool:
    """Replay the Donchian in/out state machine over the full history.
    Monotonic deques keep the prior-window max/min updates O(n) total."""
    n = len(closes)
    long_state = False
    maxq: deque[int] = deque()  # indices into highs, decreasing values
    minq: deque[int] = deque()  # indices into lows, increasing values
    for i in range(n):
        if i >= 1:  # windows cover the PRIOR bars [i-channel, i-1]
            j = i - 1
            while maxq and highs[maxq[-1]] <= highs[j]:
                maxq.pop()
            maxq.append(j)
            while minq and lows[minq[-1]] >= lows[j]:
                minq.pop()
            minq.append(j)
        while maxq and maxq[0] < i - ENTRY_CHANNEL:
            maxq.popleft()
        while minq and minq[0] < i - EXIT_CHANNEL:
            minq.popleft()
        if i < ENTRY_CHANNEL:
            continue
        if long_state and lows[i] < lows[minq[0]]:
            long_state = False
        elif not long_state and closes[i] > highs[maxq[0]]:
            long_state = True
    return long_state


def breakout_v1(bars: list[OBar]) -> Intent | None:
    if len(bars) < ENTRY_CHANNEL + 1:
        return None
    lows = [b.low for b in bars]
    if _replay_state([b.high for b in bars], lows, [b.close for b in bars]):
        return Intent(stop=min(lows[-EXIT_CHANNEL:]), target=math.inf,
                      time_stop=REEVAL)
    return None
