"""Fixture adapter: reads CSV fixtures. The default for local dev, tests, and replays."""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from atlas.dcp.market_data.models import Bar, Split


class FixtureAdapter:
    def __init__(self, root: Path) -> None:
        self._root = root

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        path = self._root / "bars" / f"{symbol}.csv"
        if not path.exists():
            return []
        out: list[Bar] = []
        with path.open() as f:
            for row in csv.DictReader(f):
                d = date.fromisoformat(row["date"])
                if start <= d <= end:
                    out.append(Bar(symbol=symbol, bar_date=d,
                                   open=Decimal(row["open"]), high=Decimal(row["high"]),
                                   low=Decimal(row["low"]), close=Decimal(row["close"]),
                                   volume=int(row["volume"])))
        return sorted(out, key=lambda b: b.bar_date)

    def fetch_splits(self, symbol: str, start: date, end: date) -> list[Split]:
        path = self._root / "splits.csv"
        if not path.exists():
            return []
        out: list[Split] = []
        with path.open() as f:
            for row in csv.DictReader(f):
                if row["symbol"] == symbol:
                    d = date.fromisoformat(row["date"])
                    if start <= d <= end:
                        out.append(Split(symbol=symbol, action_date=d,
                                         ratio=Decimal(row["ratio"])))
        return out

    def fetch_fx(self, base: str, quote: str, on: date) -> Decimal | None:
        path = self._root / "fx.csv"
        if not path.exists():
            return None
        with path.open() as f:
            for row in csv.DictReader(f):
                if (row["base"], row["quote"], row["date"]) == (base, quote, on.isoformat()):
                    return Decimal(row["rate"])
        return None

    def fetch_fx_series(self, base: str, quote: str, start: date,
                        end: date) -> dict[date, Decimal]:
        path = self._root / "fx.csv"
        out: dict[date, Decimal] = {}
        if not path.exists():
            return out
        with path.open() as f:
            for row in csv.DictReader(f):
                d = date.fromisoformat(row["date"])
                if row["base"] == base and row["quote"] == quote and start <= d <= end:
                    out[d] = Decimal(row["rate"])
        return out
