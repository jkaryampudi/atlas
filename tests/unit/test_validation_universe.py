"""The validation-instrument seeds CSV is pinned byte-for-purpose: EXACTLY the
nine original Select Sector SPDR ETFs (fixed set since 1998 — the whole point
is zero discretion, so any addition or removal is a reviewed change), all US
ETFs resolving to .US vendor codes via the UNCHANGED seeds symbol-map rules,
GICS buckets matching ADR-0007 normalisation. The signed tradable manifest
(seeds/universe.json) must not contain any of them."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from atlas.dcp.market_data.adapters.eodhd import symbol_map_from_seeds
from atlas.dcp.market_data.validation_universe import VALIDATION_SEEDS

ROOT = Path(__file__).parents[2]

SPDR_NINE = {
    "XLB": "Materials", "XLE": "Energy", "XLF": "Financials",
    "XLI": "Industrials", "XLK": "Information Technology",
    "XLP": "Consumer Staples", "XLU": "Utilities", "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
}


def _rows() -> list[dict[str, str]]:
    with VALIDATION_SEEDS.open() as f:
        return list(csv.DictReader(f))


def test_validation_seeds_are_exactly_the_nine_original_sector_spdrs():
    rows = _rows()
    assert {r["symbol"]: r["sector_gics"] for r in rows} == SPDR_NINE
    for r in rows:
        assert r["exchange"] == "NYSEARCA"
        assert r["market"] == "US"
        assert r["instrument_type"] == "etf"
        assert r["currency"] == "USD"
        assert r["economic_exposure"] == "US"


def test_validation_seeds_resolve_to_us_vendor_codes_via_seed_rules():
    assert symbol_map_from_seeds(VALIDATION_SEEDS) == {
        sym: f"{sym}.US" for sym in SPDR_NINE}


def test_validation_symbols_are_not_in_the_signed_manifest():
    manifest = {e["symbol"] for e in
                json.loads((ROOT / "seeds" / "universe.json").read_text())}
    assert manifest.isdisjoint(SPDR_NINE)
