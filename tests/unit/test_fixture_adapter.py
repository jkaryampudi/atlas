"""FixtureAdapter FX-series range behavior — pinned after a survived mutation
(review finding: removing the range filter passed the whole suite) — and the
fundamentals fixture path."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from atlas.dcp.market_data.adapters.fixture import FixtureAdapter

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_fetch_fx_series_filters_to_range():
    a = FixtureAdapter(FIXTURES)
    series = a.fetch_fx_series("USD", "AUD", date(2024, 7, 10), date(2024, 7, 15))
    # fx.csv also contains a 2026-07-10 row — it must NOT leak into a 2024 window
    assert series == {
        date(2024, 7, 10): Decimal("1.4800"),
        date(2024, 7, 11): Decimal("1.4820"),
        date(2024, 7, 12): Decimal("1.4790"),
        date(2024, 7, 15): Decimal("1.4810"),
    }


def test_fetch_fx_series_empty_outside_data():
    a = FixtureAdapter(FIXTURES)
    assert a.fetch_fx_series("USD", "AUD", date(2020, 1, 1), date(2020, 12, 31)) == {}


def test_fetch_fundamentals_reads_fixture_json_whole():
    doc = FixtureAdapter(FIXTURES).fetch_fundamentals("AVGO")
    assert doc["General"]["Code"] == "AVGO"
    assert doc["Highlights"]["MarketCapitalization"] == 1252470423552


def test_fetch_fundamentals_missing_symbol_raises_lookup_error():
    # same contract as the vendor: nothing stored is a recorded failure
    # upstream, never a silent empty snapshot
    with pytest.raises(LookupError, match="no fundamentals fixture"):
        FixtureAdapter(FIXTURES).fetch_fundamentals("ZZZZ")
