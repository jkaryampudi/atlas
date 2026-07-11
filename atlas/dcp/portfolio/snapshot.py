"""Portfolio NAV math in base currency (AUD), FX-translated per holding (Doc 03 par.1).

Pure function over positions, prices, and FX rates. The Phase 1 exit criterion requires
this to match a hand-computed fixture portfolio to the cent.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal


@dataclass(frozen=True)
class Holding:
    symbol: str
    qty: int
    currency: str        # local currency of the instrument
    last_price: Decimal  # local currency


@dataclass(frozen=True)
class Snapshot:
    nav_aud: Decimal
    cash_aud: Decimal
    holdings_value_aud: Decimal
    weights: dict[str, Decimal]  # symbol -> weight of NAV
    non_aud_exposure_pct: Decimal  # feeds risk rule L11


CENT = Decimal("0.01")


def compute_snapshot(*, cash_aud: Decimal, holdings: list[Holding],
                     fx_to_aud: dict[str, Decimal]) -> Snapshot:
    """fx_to_aud: currency -> AUD per 1 unit of that currency. Must include every
    holding currency; 'AUD' is implicitly 1."""
    rates = dict(fx_to_aud)
    rates.setdefault("AUD", Decimal(1))
    total = Decimal(0)
    non_aud = Decimal(0)
    values: dict[str, Decimal] = {}
    for h in holdings:
        if h.qty < 0:
            raise ValueError(f"short position not permitted (long-only mandate): {h.symbol}")
        if h.currency not in rates:
            raise KeyError(f"missing FX rate for {h.currency}")
        value_aud = (Decimal(h.qty) * h.last_price * rates[h.currency]).quantize(
            CENT, rounding=ROUND_HALF_EVEN)
        values[h.symbol] = values.get(h.symbol, Decimal(0)) + value_aud
        total += value_aud
        if h.currency != "AUD":
            non_aud += value_aud
    nav = (total + cash_aud).quantize(CENT, rounding=ROUND_HALF_EVEN)
    if nav <= 0:
        raise ValueError("NAV must be positive")
    weights = {s: (v / nav).quantize(Decimal("0.0001")) for s, v in values.items()}
    return Snapshot(
        nav_aud=nav,
        cash_aud=cash_aud.quantize(CENT, rounding=ROUND_HALF_EVEN),
        holdings_value_aud=total.quantize(CENT, rounding=ROUND_HALF_EVEN),
        weights=weights,
        non_aud_exposure_pct=(non_aud / nav).quantize(Decimal("0.0001")),
    )
