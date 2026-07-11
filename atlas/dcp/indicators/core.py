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


def rolling_return(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(window, len(values)):
        prev = values[i - window]
        out[i] = (values[i] / prev - 1.0) if prev else None
    return out
