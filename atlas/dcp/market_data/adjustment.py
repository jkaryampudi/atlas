"""Split adjustment for historical bars.

Bars strictly BEFORE a split's effective date are divided by the ratio (prices) and
multiplied (volume), so the series is continuous in post-split terms. Multiple splits
compound. Deterministic, pure, property-tested.
"""
from __future__ import annotations

from decimal import Decimal

from atlas.dcp.market_data.models import Bar, Split


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
