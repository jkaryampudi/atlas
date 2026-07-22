"""Split adjustment for historical bars.

Bars strictly BEFORE a split's effective date are divided by the ratio (prices) and
multiplied (volume), so the series is continuous in post-split terms. Multiple splits
compound. Deterministic, pure, property-tested.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Sequence

from atlas.dcp.market_data.models import Bar, Split


def cumulative_split_factor(splits: Sequence[Split], lo: date | None,
                            hi: date) -> Decimal:
    """The cumulative split factor needed to move a per-share quantity from the
    basis current at ``lo`` to the basis current at ``hi`` (``hi >= lo``):
    ``product(ratio)`` over splits with ``lo < action_date <= hi``.

    Boundary convention mirrors ``adjust_for_splits`` exactly (a quantity dated
    strictly BEFORE a split's action_date is pre-split): the lower bound is
    STRICT (splits at/ before ``lo`` are already baked into the ``lo`` basis) and
    the upper bound is INCLUSIVE (a split effective on ``hi`` is in force at
    ``hi``). ``lo=None`` means "-inf" (all splits with action_date <= hi).

    Splits are assumed pre-filtered to one instrument by the caller. When
    ``lo > hi`` the interval is empty and the factor is 1 (no re-basing). To keep
    look-ahead structural, callers pass only splits with action_date <= the
    knowledge date, and set ``hi`` to that same knowledge date."""
    factor = Decimal(1)
    for s in splits:
        if (lo is None or lo < s.action_date) and s.action_date <= hi:
            factor *= s.ratio
    return factor


def adjust_for_splits(bars: list[Bar], splits: list[Split]) -> list[Bar]:
    if not splits:
        return list(bars)
    relevant = sorted((s for s in splits), key=lambda s: s.action_date)
    out: list[Bar] = []
    for bar in bars:
        factor = Decimal(1)
        for s in relevant:
            if s.symbol == bar.symbol and bar.bar_date < s.action_date:
                factor *= s.ratio
        if factor == 1:
            out.append(bar)
            continue
        out.append(Bar(
            symbol=bar.symbol,
            bar_date=bar.bar_date,
            open=(bar.open / factor),
            high=(bar.high / factor),
            low=(bar.low / factor),
            close=(bar.close / factor),
            volume=int(bar.volume * factor),
            quality_flags=bar.quality_flags + ("split_adjusted",),
        ))
    return out
