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

from atlas.dcp.market_data.models import (EARNINGS_WHEN_TIMES, Bar, Dividend,
                                           EarningsEvent, Split)

BASE = "https://eodhd.com/api"


# "US" is EODHD's own umbrella venue code (index-component payloads label
# constituents that way); it is unambiguous for the .US suffix rule, and the
# symbol-map collision check still rejects dual listings.
_US_EXCHANGES = frozenset({"NYSE", "NASDAQ", "NYSEARCA", "BATS", "AMEX", "US"})


def vendor_symbol(symbol: str, exchange: str) -> str:
    """EODHD ticker code: every US venue shares the .US suffix; ASX is .AU.
    Unknown exchanges fail loudly — a silent .US default would fetch the wrong
    listing's prices without any gate noticing (review finding)."""
    if exchange == "ASX":
        return f"{symbol}.AU"
    if exchange in _US_EXCHANGES:
        return f"{symbol}.US"
    raise ValueError(f"no EODHD suffix mapping for exchange {exchange!r} ({symbol}); "
                     "add it to _US_EXCHANGES or map it explicitly")


def symbol_map_from_universe(json_path: Path) -> dict[str, str]:
    """Canonical symbol -> EODHD vendor code, from the ADR-0007 universe
    manifest (seeds/universe.json) — the manifest is the canonical universe;
    the seed CSV remains for the original nine. Same strict rules: unknown
    exchange fails loudly, dual-listed collisions refuse."""
    import json as _json
    out: dict[str, str] = {}
    for entry in _json.loads(json_path.read_text()):
        sym = entry["symbol"]
        code = vendor_symbol(sym, entry["exchange"])
        if sym in out and out[sym] != code:
            raise ValueError(f"dual-listed symbol {sym!r}: {out[sym]} vs {code} — "
                             "symbol-map keys must be unambiguous")
        out[sym] = code
    return out


def symbol_map_from_seeds(csv_path: Path) -> dict[str, str]:
    """Canonical symbol -> EODHD vendor code, from the instrument seed CSV.
    Rejects dual-listed symbol collisions instead of last-row-wins."""
    out: dict[str, str] = {}
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            sym = row["symbol"]
            code = vendor_symbol(sym, row["exchange"])
            if sym in out and out[sym] != code:
                raise ValueError(f"dual-listed symbol {sym!r}: {out[sym]} vs {code} — "
                                 "symbol-map keys must be unambiguous")
            out[sym] = code
    return out


class EodhdAdapter:
    def __init__(self, api_key: str, client: httpx.Client | None = None,
                 symbol_map: dict[str, str] | None = None) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=30)
        self._symbol_map = dict(symbol_map or {})

    def _sym(self, symbol: str) -> str:
        if not self._symbol_map:
            return symbol  # explicit vendor-code mode (caller passes AVGO.US etc.)
        try:
            return self._symbol_map[symbol]
        except KeyError:
            # Suffixless tickers resolve as US-venue at EODHD — a silent pass-
            # through would fetch the wrong instrument (review finding).
            raise ValueError(f"symbol {symbol!r} not in vendor symbol map — "
                             "refusing bare pass-through") from None

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

    def fetch_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        """GET /api/div/{code}: cash dividends by ex-date. The vendor sends two
        figures per row — `value` (retroactively split-adjusted) and
        `unadjustedValue` (the raw declared cash). We store RAW and adjust on
        read (the bars convention: backfill.py module docstring), so
        `unadjustedValue` is authoritative; `value` is only a fallback for old
        rows that lack it. Non-positive/absent amounts are vendor noise and are
        skipped — Dividend refuses them by construction."""
        rows = self._get(f"/div/{self._sym(symbol)}",
                         **{"from": start.isoformat(), "to": end.isoformat()})
        out = []
        for r in rows:
            raw = r.get("unadjustedValue")
            if not isinstance(raw, (int, float)) or raw <= 0:
                raw = r.get("value")
            if not isinstance(raw, (int, float)) or raw <= 0:
                continue
            cur = r.get("currency")
            out.append(Dividend(symbol=symbol,
                                ex_date=date.fromisoformat(str(r["date"])),
                                amount=Decimal(str(raw)),
                                currency=str(cur) if cur else None))
        return sorted(out, key=lambda d: d.ex_date)

    def fetch_earnings_calendar(self, symbol: str, start: date,
                                end: date) -> list[EarningsEvent]:
        """GET /api/calendar/earnings?symbols={code}&from=&to=: unlike /eod
        this endpoint wraps its rows in an object — the list lives under the
        "earnings" key (response shape probed live 2026-07-13; each row:
        code, report_date, date [fiscal period end], before_after_market,
        currency, actual, estimate, difference, percent). Only `report_date`
        and the closed-vocabulary `before_after_market` flag are read; a row
        whose report_date does not parse as an ISO date is vendor noise and
        is dropped. A flag outside EARNINGS_WHEN_TIMES becomes None — never
        stored, never rendered."""
        r = self._client.get(f"{BASE}/calendar/earnings",
                             params={"api_token": self._key, "fmt": "json",
                                     "symbols": self._sym(symbol),
                                     "from": start.isoformat(),
                                     "to": end.isoformat()})
        r.raise_for_status()
        data = r.json()
        rows = data.get("earnings") if isinstance(data, dict) else None
        out: list[EarningsEvent] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                day = date.fromisoformat(str(row.get("report_date")))
            except ValueError:
                continue
            flag = row.get("before_after_market")
            out.append(EarningsEvent(
                symbol=symbol, report_date=day,
                when_time=flag if flag in EARNINGS_WHEN_TIMES else None))
        return sorted(out, key=lambda e: e.report_date)

    def fetch_fundamentals(self, symbol: str) -> dict[str, object]:
        """GET /api/fundamentals/{code}: the raw vendor document (General /
        Highlights / Valuation / ... for stocks; ETF_Data for ETFs). Raises
        LookupError when EODHD has nothing — a missing document must be a
        recorded failure upstream, never a silent empty snapshot."""
        r = self._client.get(f"{BASE}/fundamentals/{self._sym(symbol)}",
                             params={"api_token": self._key, "fmt": "json"})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or not data:
            raise LookupError(f"EODHD has no fundamentals for {symbol!r}")
        return dict(data)

    def fetch_fx(self, base: str, quote: str, on: date) -> Decimal | None:
        rows = self._get(f"/eod/{base}{quote}.FOREX",
                         **{"from": on.isoformat(), "to": on.isoformat()})
        return Decimal(str(rows[0]["close"])) if rows else None

    def fetch_fx_series(self, base: str, quote: str, start: date,
                        end: date) -> dict[date, Decimal]:
        rows = self._get(f"/eod/{base}{quote}.FOREX",
                         **{"from": start.isoformat(), "to": end.isoformat()})
        return {date.fromisoformat(str(r["date"])): Decimal(str(r["close"])) for r in rows}
