"""EODHD vendor adapter (ADR-0001 decision 4). Transport-injectable for tests.

EODHD addresses instruments by vendor code (``AVGO.US``, ``NDIA.AU``); we keep
canonical seed symbols everywhere else, so the adapter owns the translation via
an optional ``symbol_map`` built from the instrument seeds.
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

from atlas.dcp.market_data.models import Bar, Split

BASE = "https://eodhd.com/api"


def vendor_symbol(symbol: str, exchange: str) -> str:
    """EODHD ticker code: every US venue shares the .US suffix; ASX is .AU."""
    return f"{symbol}.AU" if exchange == "ASX" else f"{symbol}.US"


def symbol_map_from_seeds(csv_path: Path) -> dict[str, str]:
    """Canonical symbol -> EODHD vendor code, from the instrument seed CSV."""
    with csv_path.open() as f:
        return {row["symbol"]: vendor_symbol(row["symbol"], row["exchange"])
                for row in csv.DictReader(f)}


class EodhdAdapter:
    def __init__(self, api_key: str, client: httpx.Client | None = None,
                 symbol_map: dict[str, str] | None = None) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=30)
        self._symbol_map = dict(symbol_map or {})

    def _sym(self, symbol: str) -> str:
        return self._symbol_map.get(symbol, symbol)

    def _get(self, path: str, **params: str) -> list[dict[str, object]]:
        r = self._client.get(f"{BASE}{path}",
                             params={"api_token": self._key, "fmt": "json", **params})
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        rows = self._get(f"/eod/{self._sym(symbol)}",
                         **{"from": start.isoformat(), "to": end.isoformat()})
        out = []
        for r in rows:
            out.append(Bar(symbol=symbol, bar_date=date.fromisoformat(str(r["date"])),
                           open=Decimal(str(r["open"])), high=Decimal(str(r["high"])),
                           low=Decimal(str(r["low"])), close=Decimal(str(r["close"])),
                           volume=int(str(r["volume"] or 0))))
        return sorted(out, key=lambda b: b.bar_date)

    def fetch_splits(self, symbol: str, start: date, end: date) -> list[Split]:
        rows = self._get(f"/splits/{self._sym(symbol)}",
                         **{"from": start.isoformat(), "to": end.isoformat()})
        out = []
        for r in rows:
            num, _, den = str(r["split"]).partition("/")
            ratio = Decimal(num) / Decimal(den or "1")
            out.append(Split(symbol=symbol, action_date=date.fromisoformat(str(r["date"])),
                             ratio=ratio))
        return out

    def fetch_fx(self, base: str, quote: str, on: date) -> Decimal | None:
        rows = self._get(f"/eod/{base}{quote}.FOREX",
                         **{"from": on.isoformat(), "to": on.isoformat()})
        return Decimal(str(rows[0]["close"])) if rows else None

    def fetch_fx_series(self, base: str, quote: str, start: date,
                        end: date) -> dict[date, Decimal]:
        rows = self._get(f"/eod/{base}{quote}.FOREX",
                         **{"from": start.isoformat(), "to": end.isoformat()})
        return {date.fromisoformat(str(r["date"])): Decimal(str(r["close"])) for r in rows}
