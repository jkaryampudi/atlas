"""fxlab ingest: weekday-continuity detection on fixture date lists and
vendor-client parsing through an injected transport — NO live calls
(mirrors test_eodhd_adapter.py's MockTransport style)."""
import json
from datetime import date

import httpx

from atlas.fxlab.ingest import PAIR, VENDOR_CODE, FxlabEodhdClient, missing_weekdays


def test_missing_weekdays_reports_gaps_after_first_bar_only():
    """Mon 1st, Tue 2nd, Thu 4th, Mon 8th stored: Wed 3rd and Fri 5th are
    missing weekdays; the weekend (6th/7th) is never reported; nothing
    before the first stored bar is reported."""
    dates = [date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 4), date(2024, 7, 8)]
    assert missing_weekdays(dates) == [date(2024, 7, 3), date(2024, 7, 5)]


def test_missing_weekdays_contiguous_week_is_clean():
    week = [date(2024, 7, d) for d in (1, 2, 3, 4, 5, 8)]  # Mon-Fri + next Mon
    assert missing_weekdays(week) == []


def test_missing_weekdays_degenerate_series():
    assert missing_weekdays([]) == []
    assert missing_weekdays([date(2024, 7, 3)]) == []      # single bar: no 'after'


def test_missing_weekdays_ignores_prehistory():
    """Series starting on a Wednesday: Mon/Tue before it are unknowable
    (vendor history simply starts there), not gaps."""
    assert missing_weekdays([date(2024, 7, 3), date(2024, 7, 4)]) == []


def _transport(payload, seen_paths=None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_token"] == "test-key"
        assert request.url.params["fmt"] == "json"
        if seen_paths is not None:
            seen_paths.append(request.url.path)
        return httpx.Response(200, text=json.dumps(payload))
    return httpx.MockTransport(handler)


def test_fetch_eurusd_parses_sorts_discards_volume_and_weekend_stubs():
    """Out-of-order vendor rows with volume 0 (the untrustworthy FX case) and
    thin weekend stubs (2026-07-11 is a Saturday, 07-12 a Sunday — verified
    vendor behaviour): bars come back date-sorted, Mon-Fri only, and carry NO
    volume at all."""
    payload = [
        {"date": "2026-07-10", "open": 1.0812, "high": 1.0854, "low": 1.0788,
         "close": 1.0839, "volume": 0},
        {"date": "2026-07-11", "open": 1.0839, "high": 1.0841, "low": 1.0838,
         "close": 1.0840, "volume": 0},
        {"date": "2026-07-12", "open": 1.0840, "high": 1.0844, "low": 1.0836,
         "close": 1.0841, "volume": 0},
        {"date": "2026-07-09", "open": 1.0790, "high": 1.0825, "low": 1.0761,
         "close": 1.0810, "volume": 0},
    ]
    seen: list[str] = []
    c = FxlabEodhdClient("test-key", client=httpx.Client(transport=_transport(payload, seen)))
    bars = c.fetch_eurusd(date(2026, 7, 1), date(2026, 7, 11))
    assert seen == [f"/api/eod/{VENDOR_CODE}"]
    assert [b.bar_date.day for b in bars] == [9, 10]
    assert bars[1].close == 1.0839
    assert all(not hasattr(b, "volume") for b in bars)
    assert PAIR == "EURUSD"


def test_fetch_eurusd_non_list_payload_is_empty():
    c = FxlabEodhdClient("test-key",
                         client=httpx.Client(transport=_transport({"error": "nope"})))
    assert c.fetch_eurusd(date(2026, 7, 1), date(2026, 7, 2)) == []
