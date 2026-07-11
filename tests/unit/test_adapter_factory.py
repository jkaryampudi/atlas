"""adapter_from_settings is the sole real-vs-fixture switch for every production
entrypoint; both branches must be pinned (review finding: neither was tested)."""
from pathlib import Path

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
