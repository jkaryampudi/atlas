"""Adapter selection: EODHD when a key is configured, fixtures otherwise."""
from __future__ import annotations

from pathlib import Path

from atlas.core.config import get_settings
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter, symbol_map_from_seeds
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter


def adapter_from_settings(*, fixtures_root: Path,
                          seeds_csv: Path | None = None) -> MarketDataAdapter:
    """EODHD adapter when ATLAS_EODHD_API_KEY is set (with the seed symbol map);
    the deterministic fixture adapter otherwise (per core.config)."""
    settings = get_settings()
    if settings.eodhd_api_key:
        symbol_map = symbol_map_from_seeds(seeds_csv) if seeds_csv else {}
        return EodhdAdapter(settings.eodhd_api_key, symbol_map=symbol_map)
    return FixtureAdapter(fixtures_root)
