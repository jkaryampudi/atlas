"""Vendor adapter interface. EODHD (ADR-0001) and any future vendor implement this;
the fixture adapter implements it for deterministic local development and CI."""
from __future__ import annotations

from datetime import date
from typing import Protocol

from atlas.dcp.market_data.models import Bar, Split


class MarketDataAdapter(Protocol):
    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]: ...
    def fetch_splits(self, symbol: str, start: date, end: date) -> list[Split]: ...
    def fetch_fx(self, base: str, quote: str, on: date) -> float | None: ...
