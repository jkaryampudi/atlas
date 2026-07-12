"""Vendor adapter interface. EODHD (ADR-0001) and any future vendor implement this;
the fixture adapter implements it for deterministic local development and CI."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from atlas.dcp.market_data.models import Bar, Dividend, Split


class MarketDataAdapter(Protocol):
    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]: ...
    def fetch_splits(self, symbol: str, start: date, end: date) -> list[Split]: ...
    def fetch_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]: ...
    def fetch_fx(self, base: str, quote: str, on: date) -> Decimal | None: ...
    def fetch_fx_series(self, base: str, quote: str, start: date,
                        end: date) -> dict[date, Decimal]: ...

    def fetch_fundamentals(self, symbol: str) -> dict[str, object]:
        """The vendor's RAW fundamentals document (stored whole for audit).
        Raises LookupError when the vendor has nothing for the symbol.
        SECURITY: the document contains vendor free-text (description, officer
        names) — a prompt-injection surface. Only the whitelist extractor in
        atlas/dcp/market_data/fundamentals.py may turn it into agent evidence."""
        ...
