"""Textbook candidate strategies for the FX sandbox (ADR-0008) — cited, ZERO
parameter sweeps, one version each. Each is a pure function of the visible
history ``bars[:t+1]`` returning the desired position in {-1, 0, +1}; any
internal state (channel/RSI state machines) is replayed from bar 0 on every
call, so no-look-ahead stays structural (same device as dcp/signals/trend/v1).

Most candidates are EXPECTED to fail the gauntlet (ADR-0008 Consequences);
verdicts are recorded verbatim. Parameters are the literature's, chosen
without any search — the fxlab- trial families in quant.trial_registry stay
honest for deflated Sharpe.
"""
from __future__ import annotations

from collections import deque

from atlas.fxlab.engine import FxBar, FxStrategy

# --- (a) ma_cross: golden/death cross -------------------------------------
# SMA(50) vs SMA(200), the classic golden/death-cross pair. Moving-average
# crossover rules are the canonical technical rule studied in Brock,
# Lakonishok & LeBaron (1992), "Simple Technical Trading Rules and the
# Stochastic Properties of Stock Returns", J. Finance 47(5); 50/200 is the
# standard long-term variant. Symmetric long/short (a currency pair is
# always both, ADR-0008 §4). Exact SMA tie -> flat (measure-zero on data).
FAST, SLOW = 50, 200

MA_CROSS_SPEC: dict[str, object] = {
    "family": "fxlab-ma_cross", "name": "sma_50_200_cross", "version": "1.0.0",
    "fast": FAST, "slow": SLOW, "pair": "EURUSD",
    "provenance": "textbook (golden cross; Brock/Lakonishok/LeBaron 1992); no search"}


def ma_cross(bars: list[FxBar]) -> int:
    if len(bars) < SLOW:
        return 0
    closes = [b.close for b in bars[-SLOW:]]
    sma_fast = sum(closes[-FAST:]) / FAST
    sma_slow = sum(closes) / SLOW
    if sma_fast > sma_slow:
        return 1
    if sma_fast < sma_slow:
        return -1
    return 0


# --- (b) donchian: channel breakout, Turtle System-1 shape -----------------
# Richard Donchian's channel rule as popularised by the Turtles: enter on a
# break of the previous 20-day extreme, exit on a break of the opposite
# 10-day extreme (Curtis Faith, "Way of the Turtle", 2007, System 1: 20-day
# entry / 10-day opposite exit). Daily-close breakouts of the PRIOR N bars'
# high/low channel (intraday penetration is not observable on daily bars).
# Exit is evaluated before entry on the same bar, so a close through the
# opposite 20-day extreme exits and reverses in one session — symmetric.
ENTRY_WINDOW, EXIT_WINDOW = 20, 10

DONCHIAN_SPEC: dict[str, object] = {
    "family": "fxlab-donchian", "name": "donchian_20_10", "version": "1.0.0",
    "entry_window": ENTRY_WINDOW, "exit_window": EXIT_WINDOW, "pair": "EURUSD",
    "provenance": "textbook (Donchian channel; Faith 2007 Turtle System 1); no search"}


def _push_max(dq: deque[tuple[int, float]], i: int, v: float) -> None:
    while dq and dq[-1][1] <= v:
        dq.pop()
    dq.append((i, v))


def _push_min(dq: deque[tuple[int, float]], i: int, v: float) -> None:
    while dq and dq[-1][1] >= v:
        dq.pop()
    dq.append((i, v))


def donchian(bars: list[FxBar]) -> int:
    """Replay the channel state machine from bar 0 (monotonic deques keep the
    replay O(n)). Channels at bar i cover the PRIOR bars [i-w, i-1]."""
    pos = 0
    hi_e: deque[tuple[int, float]] = deque()
    lo_e: deque[tuple[int, float]] = deque()
    hi_x: deque[tuple[int, float]] = deque()
    lo_x: deque[tuple[int, float]] = deque()
    for i, b in enumerate(bars):
        if i > 0:
            prev = bars[i - 1]
            _push_max(hi_e, i - 1, prev.high)
            _push_min(lo_e, i - 1, prev.low)
            _push_max(hi_x, i - 1, prev.high)
            _push_min(lo_x, i - 1, prev.low)
        while hi_e and hi_e[0][0] < i - ENTRY_WINDOW:
            hi_e.popleft()
        while lo_e and lo_e[0][0] < i - ENTRY_WINDOW:
            lo_e.popleft()
        while hi_x and hi_x[0][0] < i - EXIT_WINDOW:
            hi_x.popleft()
        while lo_x and lo_x[0][0] < i - EXIT_WINDOW:
            lo_x.popleft()
        if i < ENTRY_WINDOW:
            continue
        if pos == 1 and b.close < lo_x[0][1]:
            pos = 0
        elif pos == -1 and b.close > hi_x[0][1]:
            pos = 0
        if pos == 0:
            if b.close > hi_e[0][1]:
                pos = 1
            elif b.close < lo_e[0][1]:
                pos = -1
    return pos


# --- (c) rsi_fade: Wilder RSI(14) contrarian --------------------------------
# J. Welles Wilder (1978), "New Concepts in Technical Trading Systems":
# 14-period RSI with Wilder smoothing and the 30/70 bands. The classic
# contrarian usage: fade oversold (<30 -> long) and overbought (>70 ->
# short), exit at the 50 midline cross. Exit is evaluated before entry on
# the same bar. Degenerate windows: no losses -> RSI 100 (Wilder); a fully
# flat window (no gains AND no losses) is neutral (50), never a signal.
RSI_WINDOW = 14
OVERSOLD, OVERBOUGHT, MIDLINE = 30.0, 70.0, 50.0

RSI_FADE_SPEC: dict[str, object] = {
    "family": "fxlab-rsi_fade", "name": "rsi14_30_70_fade", "version": "1.0.0",
    "rsi_window": RSI_WINDOW, "oversold": OVERSOLD, "overbought": OVERBOUGHT,
    "midline": MIDLINE, "pair": "EURUSD",
    "provenance": "textbook (Wilder 1978 RSI, 30/70 bands, midline exit); no search"}


def rsi_fade(bars: list[FxBar]) -> int:
    """Replay Wilder RSI and the fade state machine from bar 0 — O(n)."""
    if len(bars) < RSI_WINDOW + 1:
        return 0
    closes = [b.close for b in bars]
    pos = 0
    sum_g = sum_l = 0.0
    avg_g = avg_l = 0.0
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gain, loss = max(chg, 0.0), max(-chg, 0.0)
        if i <= RSI_WINDOW:
            sum_g += gain
            sum_l += loss
            if i < RSI_WINDOW:
                continue
            avg_g, avg_l = sum_g / RSI_WINDOW, sum_l / RSI_WINDOW
        else:
            avg_g = (avg_g * (RSI_WINDOW - 1) + gain) / RSI_WINDOW
            avg_l = (avg_l * (RSI_WINDOW - 1) + loss) / RSI_WINDOW
        if avg_l == 0.0:
            rsi = MIDLINE if avg_g == 0.0 else 100.0
        else:
            rsi = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
        if pos == 1 and rsi >= MIDLINE:
            pos = 0
        elif pos == -1 and rsi <= MIDLINE:
            pos = 0
        if pos == 0:
            if rsi < OVERSOLD:
                pos = 1
            elif rsi > OVERBOUGHT:
                pos = -1
    return pos


CANDIDATES: dict[str, tuple[FxStrategy, dict[str, object]]] = {
    "ma_cross": (ma_cross, MA_CROSS_SPEC),
    "donchian": (donchian, DONCHIAN_SPEC),
    "rsi_fade": (rsi_fade, RSI_FADE_SPEC),
}

# Longest lookback across candidates: the gauntlet's warmup. At t = WARMUP the
# strategy sees exactly SLOW bars — ma_cross's first defined signal.
WARMUP = SLOW
