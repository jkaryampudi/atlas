"""EODHD vendor adapter (ADR-0001 decision 4). Transport-injectable for tests."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx

from atlas.dcp.market_data.models import Bar, Split

BASE = "https://eodhd.com/api"


class EodhdAdapter:
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=30)

    def _get(self, path: str, **params: str) -> list[dict[str, object]]:
        r = self._client.get(f"{BASE}{path}",
                             params={"api_token": self._key, "fmt": "json", **params})
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        rows = self._get(f"/eod/{symbol}", **{"from": start.isoformat(), "to": end.isoformat()})
        out = []
        for r in rows:
            out.append(Bar(symbol=symbol, bar_date=date.fromisoformat(str(r["date"])),
                           open=Decimal(str(r["open"])), high=Decimal(str(r["high"])),
                           low=Decimal(str(r["low"])), close=Decimal(str(r["close"])),
                           volume=int(str(r["volume"] or 0))))
        return sorted(out, key=lambda b: b.bar_date)

    def fetch_splits(self, symbol: str, start: date, end: date) -> list[Split]:
        rows = self._get(f"/splits/{symbol}", **{"from": start.isoformat(), "to": end.isoformat()})
        out = []
        for r in rows:
            num, _, den = str(r["split"]).partition("/")
            ratio = Decimal(num) / Decimal(den or "1")
            out.append(Split(symbol=symbol, action_date=date.fromisoformat(str(r["date"])),
                             ratio=ratio))
        return out

    def fetch_fx(self, base: str, quote: str, on: date) -> float | None:
        rows = self._get(f"/eod/{base}{quote}.FOREX",
                         **{"from": on.isoformat(), "to": on.isoformat()})
        return float(str(rows[0]["close"])) if rows else None
