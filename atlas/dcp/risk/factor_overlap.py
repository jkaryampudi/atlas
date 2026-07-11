"""Factor-overlap guard (Doc 04 §12, ADR-0002/0003 amendment).

In addition to L8, the risk engine decomposes pro-forma holdings into market /
sector / momentum loadings; a new position is rejected if it raises any single
factor loading above its registered cap. Two momentum names in one sector are
one bet — the engine must see that, even when each name clears L1/L3/L8 alone.

The caps here are v1 SEEDS pending formal registration in risk.limit_sets v2
(§10 change control): market 1.0 (NAV-weighted beta; long-only book capped at
0.80 gross by §11 leaves headroom to ~beta-1.25), sector 0.25 (NAV weight per
GICS sector, aligned with L3), momentum 0.5 (NAV-weighted momentum loading).
They are conservative engineering placeholders — do not read calibrated
precision into them.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from atlas.dcp.risk.engine import _BROAD_SECTOR, RuleResult

_ZERO = Decimal("0")


@dataclass(frozen=True)
class FactorLoadings:
    """Per-name factor view; loadings are NAV-weighted by value_aud when the
    pro-forma book is aggregated. Momentum is the strategy's standardised
    momentum score (can exceed 1), market_beta the regression beta."""
    symbol: str
    value_aud: Decimal
    market_beta: Decimal
    sector_gics: str
    momentum: Decimal


@dataclass(frozen=True)
class FactorCaps:
    """v1 seed caps — see module docstring; pending limit_sets v2 registration."""
    market: Decimal = Decimal("1.0")
    sector: Decimal = Decimal("0.25")
    momentum: Decimal = Decimal("0.5")


def check_factor_overlap(proposal: FactorLoadings,
                         holdings: Sequence[FactorLoadings], *,
                         nav_aud: Decimal,
                         caps: FactorCaps = FactorCaps()) -> RuleResult:
    """§12: pro-forma NAV-weighted loading per factor; reject if any single
    factor loading exceeds its cap. Always itemises every loading — like
    engine.validate, a FAIL must explain itself completely."""
    if nav_aud <= 0:
        raise ValueError("NAV must be positive")
    book = (*tuple(holdings), proposal)
    market = sum((h.value_aud / nav_aud * h.market_beta for h in book), _ZERO)
    momentum = sum((h.value_aud / nav_aud * h.momentum for h in book), _ZERO)
    sector_w: dict[str, Decimal] = {}
    for h in book:
        if h.sector_gics != _BROAD_SECTOR:  # Broad ETFs are not a sector bet (L3)
            sector_w[h.sector_gics] = (sector_w.get(h.sector_gics, _ZERO)
                                       + h.value_aud / nav_aud)

    breaches: list[str] = []
    if market > caps.market:
        breaches.append(f"market {market:.4f} > cap {caps.market}")
    for sector in sorted(sector_w):
        if sector_w[sector] > caps.sector:
            breaches.append(f"sector {sector} {sector_w[sector]:.4f} "
                            f"> cap {caps.sector}")
    if momentum > caps.momentum:
        breaches.append(f"momentum {momentum:.4f} > cap {caps.momentum}")

    if sector_w:
        top = max(sorted(sector_w), key=lambda s: sector_w[s])
        sector_summary = f"max sector {top} {sector_w[top]:.4f} vs cap {caps.sector}"
    else:
        sector_summary = "no sector exposure (Broad only)"
    detail = (f"market {market:.4f} vs cap {caps.market}, "
              f"momentum {momentum:.4f} vs cap {caps.momentum}, {sector_summary}")
    if breaches:
        detail += "; BREACH: " + "; ".join(breaches)
    return RuleResult("FACTOR", not breaches, detail)
