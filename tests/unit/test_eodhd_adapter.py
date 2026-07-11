import json
from datetime import date
from decimal import Decimal

import httpx

from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter, vendor_symbol


def _transport(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_token"] == "test-key"
        return httpx.Response(200, text=json.dumps(payload))
    return httpx.MockTransport(handler)


def test_fetch_bars_parses_and_sorts():
    payload = [
        {"date": "2026-07-10", "open": 172.1, "high": 174.0, "low": 171.0,
         "close": 173.5, "volume": 1000},
        {"date": "2026-07-09", "open": 170.0, "high": 172.5, "low": 169.5,
         "close": 172.0, "volume": 900},
    ]
    a = EodhdAdapter("test-key", client=httpx.Client(transport=_transport(payload)))
    bars = a.fetch_bars("AVGO.US", date(2026, 7, 1), date(2026, 7, 11))
    assert [b.bar_date.day for b in bars] == [9, 10]
    assert bars[1].close == Decimal("173.5")


def test_fetch_splits_parses_ratio():
    a = EodhdAdapter("test-key", client=httpx.Client(
        transport=_transport([{"date": "2024-07-15", "split": "10/1"}])))
    splits = a.fetch_splits("AVGO.US", date(2024, 1, 1), date(2024, 12, 31))
    assert splits[0].ratio == Decimal("10")


def _url_capturing_transport(payload, seen_paths):
    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, text=json.dumps(payload))
    return httpx.MockTransport(handler)


def test_symbol_map_translates_vendor_code_but_keeps_canonical_symbol():
    seen: list[str] = []
    payload = [{"date": "2026-07-10", "open": 100.0, "high": 101.0, "low": 99.0,
                "close": 100.5, "volume": 10}]
    a = EodhdAdapter("test-key", client=httpx.Client(
        transport=_url_capturing_transport(payload, seen)),
        symbol_map={"NDIA": "NDIA.AU"})
    bars = a.fetch_bars("NDIA", date(2026, 7, 1), date(2026, 7, 11))
    assert seen == ["/api/eod/NDIA.AU"]          # vendor sees the mapped code
    assert bars[0].symbol == "NDIA"              # we keep the canonical symbol


def test_vendor_symbol_mapping():
    assert vendor_symbol("SPY", "NYSEARCA") == "SPY.US"
    assert vendor_symbol("AVGO", "NASDAQ") == "AVGO.US"
    assert vendor_symbol("NDIA", "ASX") == "NDIA.AU"


def test_fetch_fx_returns_decimal():
    a = EodhdAdapter("test-key", client=httpx.Client(transport=_transport(
        [{"date": "2026-07-10", "close": 1.52}])))
    assert a.fetch_fx("USD", "AUD", date(2026, 7, 10)) == Decimal("1.52")


def test_fetch_fx_missing_day_returns_none():
    a = EodhdAdapter("test-key", client=httpx.Client(transport=_transport([])))
    assert a.fetch_fx("USD", "AUD", date(2026, 7, 12)) is None


def test_fetch_fx_series_parses_range():
    seen: list[str] = []
    payload = [{"date": "2024-07-10", "close": 1.50},
               {"date": "2024-07-11", "close": 1.51}]
    a = EodhdAdapter("test-key", client=httpx.Client(
        transport=_url_capturing_transport(payload, seen)))
    series = a.fetch_fx_series("USD", "AUD", date(2024, 7, 10), date(2024, 7, 12))
    assert seen == ["/api/eod/USDAUD.FOREX"]
    assert series == {date(2024, 7, 10): Decimal("1.50"),
                      date(2024, 7, 11): Decimal("1.51")}
