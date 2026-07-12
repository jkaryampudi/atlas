"""Adapter selection: EODHD when a key is configured, fixtures otherwise."""
from __future__ import annotations

from pathlib import Path

from atlas.core.config import get_settings
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.adapters.eodhd import (EodhdAdapter, symbol_map_from_seeds,
                                                   symbol_map_from_universe)
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter


def adapter_from_settings(*, fixtures_root: Path,
                          seeds_csv: Path | None = None,
                          universe_json: Path | None = None,
                          extra_seeds_csv: Path | None = None) -> MarketDataAdapter:
    """EODHD adapter when ATLAS_EODHD_API_KEY is set (with the seed symbol map);
    the deterministic fixture adapter otherwise (per core.config).

    ``extra_seeds_csv`` merges one more seeds-shaped CSV into the symbol map
    (same strict collision rules) — the documented route for validation-only
    instruments (seeds/validation_instruments.csv), which live outside both
    the instrument seeds and the SIGNED universe manifest."""
    settings = get_settings()
    if settings.eodhd_api_key:
        symbol_map = symbol_map_from_seeds(seeds_csv) if seeds_csv else {}
        if universe_json is None and seeds_csv is not None:
            candidate = seeds_csv.parent / "universe.json"
            universe_json = candidate if candidate.exists() else None
        if universe_json is not None:
            for sym, code in symbol_map_from_universe(universe_json).items():
                if sym in symbol_map and symbol_map[sym] != code:
                    raise ValueError(f"symbol map conflict for {sym!r}: "
                                     f"{symbol_map[sym]} (seeds) vs {code} (universe)")
                symbol_map[sym] = code
        if extra_seeds_csv is not None:
            for sym, code in symbol_map_from_seeds(extra_seeds_csv).items():
                if sym in symbol_map and symbol_map[sym] != code:
                    raise ValueError(f"symbol map conflict for {sym!r}: "
                                     f"{symbol_map[sym]} (existing) vs {code} "
                                     "(extra seeds)")
                symbol_map[sym] = code
        return EodhdAdapter(settings.eodhd_api_key, symbol_map=symbol_map)
    return FixtureAdapter(fixtures_root)
