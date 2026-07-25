"""Shared currency/unit normalisation layer for financial RATIOS (F-010).

A ratio that divides a financial-STATEMENT figure (reported in the issuer's
accounting currency, ``General.CurrencyCode``) by a MARKET figure (price or
market cap, in the LISTING currency, ``instruments.currency``) is only
meaningful when the two currencies agree. For a non-USD reporter listed on a USD
venue (or an ADR), dividing the two produces a number scaled by the FX rate — a
fabricated "yield" that looks real. The review named FCF yield; the same trap sits
under earnings yield, book-to-market, EV ratios, revenue yield, dividend yield,
and fair-value upside wherever Atlas forms the ratio itself.

This module is the ONE place the currency compatibility rule lives, so the memo
scorecard's dossier (financials_panel), the valuation panel (valuation_models),
and the universe health score (health_score) all fail closed the same way:

  * ``cross_currency_ratio`` — the fail-closed ratio. Returns
    ``(value, blocked)``. It computes ``num/den`` ONLY when the two currencies are
    CONFIRMED equal (both known and identical); when they are known-and-different
    OR either is unknown it returns ``(None, True)`` — an EXPLICIT currency block,
    never a silent zero and never a mixed number. A missing/zero input is an
    ordinary absence ``(None, False)`` — not a currency block.
  * ``currencies_incompatible`` — the weaker predicate used where a whole panel is
    blocked on a KNOWN mismatch only (valuation_models keeps computing on unknown,
    its long-standing behaviour); centralised here so the normalisation is not
    re-implemented per module.

Only ISO-4217-shaped codes are admitted; anything else normalises to None
(unknown) and therefore fails the ratio closed.
"""
from __future__ import annotations

import re

_ISO4217 = re.compile(r"[A-Z]{3}")


def norm_currency(value: object) -> str | None:
    """A currency code admitted only as an ISO-4217-shaped 3-letter code
    (upper-cased, trimmed); anything else -> None (unknown)."""
    if value is None:
        return None
    s = str(value).strip().upper()
    return s if _ISO4217.fullmatch(s) else None


def currencies_confirmed_same(reporting: object, listing: object) -> bool:
    """True ONLY when both currencies are known and identical — the condition
    under which a statement/market ratio is currency-safe to compute."""
    r, listed = norm_currency(reporting), norm_currency(listing)
    return r is not None and listed is not None and r == listed


def currencies_incompatible(reporting: object, listing: object) -> bool:
    """True ONLY when both currencies are known AND differ (a proven mismatch).
    Unknown-on-either-side is not 'incompatible' here — the weaker rule a
    whole-panel block uses so it does not withdraw from every unknown reporter."""
    r, listed = norm_currency(reporting), norm_currency(listing)
    return r is not None and listed is not None and r != listed


def cross_currency_ratio(numerator: float | None, denominator: float | None, *,
                         num_ccy: object, den_ccy: object,
                         scale: float = 1.0) -> tuple[float | None, bool]:
    """The fail-closed statement/market ratio (F-010). Returns ``(value, blocked)``:

      * ``(None, False)`` when an input is missing or the denominator is zero —
        an ordinary absence, not a currency problem;
      * ``(None, True)`` when the currencies are NOT confirmed equal — an explicit
        currency block (known-different OR either unknown), never a mixed number;
      * ``(numerator/denominator*scale, False)`` when the currencies are confirmed
        equal — the only case a real ratio is emitted.
    """
    if numerator is None or not denominator:
        return None, False
    if not currencies_confirmed_same(num_ccy, den_ccy):
        return None, True
    return numerator / denominator * scale, False
