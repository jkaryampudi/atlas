import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from atlas.dcp.market_data.adapters.eodhd import (EodhdAdapter, symbol_map_from_seeds,
                                                  vendor_symbol)


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


def _url_capturing_transport(payload, seen_paths, seen_params=None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if seen_params is not None:
            seen_params.append(dict(request.url.params))
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


def test_fetch_fx_series_parses_range_and_sends_bounds():
    seen: list[str] = []
    params: list[dict] = []
    payload = [{"date": "2024-07-10", "close": 1.50},
               {"date": "2024-07-11", "close": 1.51}]
    a = EodhdAdapter("test-key", client=httpx.Client(
        transport=_url_capturing_transport(payload, seen, params)))
    series = a.fetch_fx_series("USD", "AUD", date(2024, 7, 10), date(2024, 7, 12))
    assert seen == ["/api/eod/USDAUD.FOREX"]
    # range params must reach the vendor — dropping them fetches ALL history
    assert params[0]["from"] == "2024-07-10"
    assert params[0]["to"] == "2024-07-12"
    assert series == {date(2024, 7, 10): Decimal("1.50"),
                      date(2024, 7, 11): Decimal("1.51")}


def test_fetch_fundamentals_hits_vendor_path_with_mapped_code():
    seen: list[str] = []
    params: list[dict] = []
    payload = {"General": {"Code": "AVGO"}, "Highlights": {"PERatio": 39.7}}
    a = EodhdAdapter("test-key", client=httpx.Client(
        transport=_url_capturing_transport(payload, seen, params)),
        symbol_map={"AVGO": "AVGO.US"})
    doc = a.fetch_fundamentals("AVGO")
    assert seen == ["/api/fundamentals/AVGO.US"]  # vendor sees the mapped code
    assert params[0]["api_token"] == "test-key"
    assert doc["General"]["Code"] == "AVGO"       # raw document, whole


def test_fetch_fundamentals_empty_document_raises_lookup_error():
    a = EodhdAdapter("test-key", client=httpx.Client(transport=_transport({})))
    with pytest.raises(LookupError, match="no fundamentals"):
        a.fetch_fundamentals("AVGO.US")


def test_fetch_fundamentals_non_object_raises_lookup_error():
    a = EodhdAdapter("test-key", client=httpx.Client(transport=_transport([])))
    with pytest.raises(LookupError, match="no fundamentals"):
        a.fetch_fundamentals("AVGO.US")


def test_fetch_fundamentals_unmapped_symbol_refuses_bare_passthrough():
    a = EodhdAdapter("test-key", client=httpx.Client(transport=_transport({})),
                     symbol_map={"NDIA": "NDIA.AU"})
    with pytest.raises(ValueError, match="not in vendor symbol map"):
        a.fetch_fundamentals("SPY")


def test_unmapped_symbol_with_map_refuses_bare_passthrough():
    a = EodhdAdapter("test-key", client=httpx.Client(transport=_transport([])),
                     symbol_map={"NDIA": "NDIA.AU"})
    with pytest.raises(ValueError, match="not in vendor symbol map"):
        a.fetch_bars("SPY", date(2026, 7, 1), date(2026, 7, 11))


def test_vendor_symbol_unknown_exchange_raises():
    with pytest.raises(ValueError, match="no EODHD suffix mapping"):
        vendor_symbol("RELIANCE", "NSE")


def test_symbol_map_from_seeds_builds_and_rejects_collisions(tmp_path):
    good = tmp_path / "seeds.csv"
    good.write_text("symbol,exchange\nSPY,NYSEARCA\nNDIA,ASX\n")
    assert symbol_map_from_seeds(good) == {"SPY": "SPY.US", "NDIA": "NDIA.AU"}
    dual = tmp_path / "dual.csv"
    dual.write_text("symbol,exchange\nNDIA,ASX\nNDIA,NYSE\n")
    with pytest.raises(ValueError, match="dual-listed"):
        symbol_map_from_seeds(dual)
