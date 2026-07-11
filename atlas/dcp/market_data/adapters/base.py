"""Vendor adapter interface. EODHD (ADR-0001) and any future vendor implement this;
the fixture adapter implements it for deterministic local development and CI."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from atlas.dcp.market_data.models import Bar, Split


class MarketDataAdapter(Protocol):
    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]: ...
    def fetch_splits(self, symbol: str, start: date, end: date) -> list[Split]: ...
    def fetch_fx(self, base: str, quote: str, on: date) -> Decimal | None: ...
    def fetch_fx_series(self, base: str, quote: str, start: date,
                        end: date) -> dict[date, Decimal]: ...
