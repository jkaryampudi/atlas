import json
from datetime import date
from decimal import Decimal

import httpx

from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter


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
