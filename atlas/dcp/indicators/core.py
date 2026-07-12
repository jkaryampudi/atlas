"""Pure indicator functions over close/OHLC lists. No I/O, no state, property-tested."""
from __future__ import annotations


def sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if window <= 0:
        raise ValueError("window must be positive")
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= window:
            run -= values[i - window]
        if i >= window - 1:
            out[i] = run / window
    return out


def atr(highs: list[float], lows: list[float], closes: list[float],
        window: int = 14) -> list[float | None]:
    n = len(closes)
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(highs[i] - lows[i],
                           abs(highs[i] - closes[i - 1]),
                           abs(lows[i] - closes[i - 1])))
    return sma(trs, window)


def wilder_atr(highs: list[float], lows: list[float], closes: list[float],
               period: int = 14) -> list[float | None]:
    """Wilder's ATR (ADR-0006 stop derivation). True range per bar is
    max(h-l, |h-prev_close|, |l-prev_close|) — the first bar has no previous
    close, so its TR is h-l. The first ATR value (index period-1) is the
    simple mean of the first `period` TRs; every later value is Wilder-
    smoothed: atr = (prev*(period-1) + tr) / period. None until warm."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(closes)
    out: list[float | None] = [None] * n
    atr_val = 0.0
    for i in range(n):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
        if i < period - 1:
            atr_val += tr                      # accumulating the seed mean
        elif i == period - 1:
            atr_val = (atr_val + tr) / period  # seed: simple mean of TRs
            out[i] = atr_val
        else:
            atr_val = (atr_val * (period - 1) + tr) / period
            out[i] = atr_val
    return out


def rolling_return(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(window, len(values)):
        prev = values[i - window]
        out[i] = (values[i] / prev - 1.0) if prev else None
    return out
