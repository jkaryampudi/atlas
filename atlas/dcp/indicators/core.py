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


def wilder_avg_gain_loss(values: list[float],
                         period: int) -> list[tuple[float, float] | None]:
    """Wilder-smoothed average gain/loss pairs (the RSI internals, exposed so a
    strategy can derive price thresholds from the same smoothing — one
    implementation, no drift). Change per bar is close-to-close; the seed at
    index `period` is the simple mean of the first `period` gains/losses; every
    later value is Wilder-smoothed: avg = (prev*(period-1) + x) / period.
    None until warm (index >= period)."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: list[tuple[float, float] | None] = [None] * n
    ag = al = 0.0
    for i in range(1, n):
        change = values[i] - values[i - 1]
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if i < period:
            ag += gain      # accumulating the seed mean (warm at index == period)
            al += loss
        elif i == period:
            ag = (ag + gain) / period
            al = (al + loss) / period
            out[i] = (ag, al)
        else:
            ag = (ag * (period - 1) + gain) / period
            al = (al * (period - 1) + loss) / period
            out[i] = (ag, al)
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """Wilder RSI over closes. 100 when avg loss is zero, 0 when avg gain is
    zero, 50 when both are zero (flat series); None until warm."""
    out: list[float | None] = [None] * len(values)
    for i, pair in enumerate(wilder_avg_gain_loss(values, period)):
        if pair is None:
            continue
        ag, al = pair
        if ag == 0.0 and al == 0.0:
            out[i] = 50.0
        elif al == 0.0:
            out[i] = 100.0
        else:
            out[i] = 100.0 * ag / (ag + al)
    return out


def rolling_return(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(window, len(values)):
        prev = values[i - window]
        out[i] = (values[i] / prev - 1.0) if prev else None
    return out
