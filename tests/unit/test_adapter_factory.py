"""adapter_from_settings is the sole real-vs-fixture switch for every production
entrypoint; both branches must be pinned (review finding: neither was tested)."""
from pathlib import Path

import pytest

from atlas.dcp.market_data.adapters import adapter_from_settings
from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SEEDS = ROOT / "seeds" / "instruments_seed.csv"


def test_key_present_returns_eodhd_with_seed_symbol_map(monkeypatch):
    monkeypatch.setenv("ATLAS_EODHD_API_KEY", "test-key")
    a = adapter_from_settings(fixtures_root=FIXTURES, seeds_csv=SEEDS)
    assert isinstance(a, EodhdAdapter)
    # the map must resolve seed symbols to vendor codes (bare pass-through is refused)
    assert a._sym("SPY") == "SPY.US"
    assert a._sym("NDIA") == "NDIA.AU"


def test_key_absent_returns_fixture_adapter(monkeypatch):
    # env beats .env in pydantic-settings, so an empty var forces the fixture branch
    monkeypatch.setenv("ATLAS_EODHD_API_KEY", "")
    a = adapter_from_settings(fixtures_root=FIXTURES, seeds_csv=SEEDS)
    assert isinstance(a, FixtureAdapter)


def test_extra_seeds_csv_merges_validation_symbols(monkeypatch):
    from atlas.dcp.market_data.validation_universe import VALIDATION_SEEDS

    monkeypatch.setenv("ATLAS_EODHD_API_KEY", "test-key")
    a = adapter_from_settings(fixtures_root=FIXTURES, seeds_csv=SEEDS,
                              extra_seeds_csv=VALIDATION_SEEDS)
    assert isinstance(a, EodhdAdapter)
    assert a._sym("XLB") == "XLB.US"      # validation-only symbol resolves
    assert a._sym("SPY") == "SPY.US"      # tradable map still intact


def test_extra_seeds_csv_collision_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_EODHD_API_KEY", "test-key")
    conflicting = tmp_path / "extra.csv"
    conflicting.write_text(
        "symbol,exchange,market,instrument_type,name,sector_gics,currency,"
        "economic_exposure\n"
        "SPY,ASX,AU,etf,Wrong Venue,Broad,AUD,AU\n")
    with pytest.raises(ValueError, match="symbol map conflict for 'SPY'"):
        adapter_from_settings(fixtures_root=FIXTURES, seeds_csv=SEEDS,
                              extra_seeds_csv=conflicting)
