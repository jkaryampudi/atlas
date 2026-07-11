"""L8 correlation feed (Doc 04 §3 L8): 90-session pairwise Pearson correlation
of daily simple returns, computed from market.price_bars_daily and shaped for
engine.validate's `corr_with_existing` input.

FAIL-CLOSED: when a pair has fewer than MIN_OVERLAP_RETURNS overlapping return
observations — or degenerate data (non-positive closes, zero variance) — the
pair is reported as Decimal("1"): perfectly correlated, the worst case.
engine.validate's L8 treats a MISSING correlation as no-block, so omitting
thin pairs would silently fail open; this project fails closed (CLAUDE.md
invariant 3: no code path may weaken a risk check). A worst-case 1 makes L8
bite exactly when the combined weight alone would breach the cap.

NO LOOK-AHEAD: only bars with bar_date <= end enter the window, and the window
is the most recent `window_sessions` bars, never future ones.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

MIN_OVERLAP_RETURNS = 60      # below this, a 90-session estimate is noise
PRICE_SOURCE = "EodhdAdapter"  # real vendor bars only, never fixtures
_QUANT = Decimal("0.0001")
_WORST_CASE = Decimal("1")    # fail-closed: assume perfectly correlated


def pairwise_correlation(a: Sequence[Decimal] | Sequence[float],
                         b: Sequence[Decimal] | Sequence[float]) -> Decimal:
    """Pearson correlation of two ALIGNED daily simple-return series. Float
    math internally — 28-digit Decimal precision is spurious for an estimated
    statistic — with the result clamped to [-1, 1] and quantized to 0.0001.
    Degenerate input (misalignment, n < 2, zero variance) raises: this is the
    pure math; fail-closed policy lives in the callers."""
    if len(a) != len(b):
        raise ValueError("return series must be aligned (equal length)")
    n = len(a)
    if n < 2:
        raise ValueError("need at least 2 return observations")
    xs = [float(v) for v in a]
    ys = [float(v) for v in b]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0.0 or vy == 0.0:
        raise ValueError("zero variance — correlation undefined")
    r = max(-1.0, min(1.0, cov / math.sqrt(vx * vy)))
    return Decimal(str(r)).quantize(_QUANT)


def _pair_correlation(closes_a: Mapping[date, Decimal],
                      closes_b: Mapping[date, Decimal]) -> Decimal:
    """One pair from date->close maps, aligned on common bar dates (US and
    India calendars differ; returns across a one-sided gap span the gap).
    Fail-closed to Decimal("1") — see module docstring — on thin overlap,
    non-positive closes, or zero variance."""
    common = sorted(set(closes_a) & set(closes_b))
    if len(common) < MIN_OVERLAP_RETURNS + 1:  # k returns need k+1 closes
        return _WORST_CASE
    a = [float(closes_a[d]) for d in common]
    b = [float(closes_b[d]) for d in common]
    if min(a) <= 0.0 or min(b) <= 0.0:
        return _WORST_CASE
    returns_a = [a[i] / a[i - 1] - 1.0 for i in range(1, len(a))]
    returns_b = [b[i] / b[i - 1] - 1.0 for i in range(1, len(b))]
    try:
        return pairwise_correlation(returns_a, returns_b)
    except ValueError:  # zero variance
        return _WORST_CASE


_CLOSES_SQL = text(
    "SELECT b.bar_date, b.close "
    "FROM market.price_bars_daily b "
    "JOIN market.instruments i ON i.id = b.instrument_id "
    "WHERE i.symbol = :symbol AND b.source = :source "
    "  AND b.bar_date <= :end AND b.close IS NOT NULL "
    "ORDER BY b.bar_date DESC "
    "LIMIT :window")


def _load_closes(session: Session, symbol: str, *, end: date,
                 window: int) -> dict[date, Decimal]:
    rows = session.execute(_CLOSES_SQL, {"symbol": symbol, "source": PRICE_SOURCE,
                                         "end": end, "window": window}).all()
    return {row.bar_date: Decimal(row.close) for row in rows}


def correlations_with_existing(session: Session, candidate_symbol: str,
                               existing_symbols: Sequence[str], *, end: date,
                               window_sessions: int = 90) -> dict[str, Decimal]:
    """90-session correlations of the candidate vs each existing holding —
    the L8 `corr_with_existing` input for engine.validate. Windows are the
    most recent `window_sessions` bars on or before `end` (no look-ahead);
    every existing symbol appears in the result, thin or missing pairs as the
    fail-closed worst case Decimal("1")."""
    cand = _load_closes(session, candidate_symbol, end=end, window=window_sessions)
    return {sym: _pair_correlation(cand, _load_closes(session, sym, end=end,
                                                      window=window_sessions))
            for sym in existing_symbols}
