"""The universe manifest (seeds/universe.json) is the reviewed source of truth
for "what do we trade" (ADR-0007: S&P 100 snapshot + India sleeve + the nine
originals). The instrument seed CSV rows must appear in it VERBATIM — the
manifest may exceed the CSV (that is the ADR-0007 expansion), never drift
from it."""
import csv
import json
from pathlib import Path

from atlas.dcp.market_data.universe import REQUIRED_FIELDS

ROOT = Path(__file__).parents[2]


def _manifest() -> list[dict[str, object]]:
    entries = json.loads((ROOT / "seeds" / "universe.json").read_text())
    assert isinstance(entries, list)
    return entries


def test_manifest_contains_every_seed_csv_row_verbatim():
    with (ROOT / "seeds" / "instruments_seed.csv").open() as f:
        csv_rows = {(r["symbol"], r["exchange"]): r for r in csv.DictReader(f)}
    entries = _manifest()
    by_key = {(str(e["symbol"]), str(e["exchange"])): e for e in entries}
    assert set(csv_rows) <= set(by_key)          # ADR-0007: superset, never drift
    assert len(entries) >= 112                    # the expanded universe
    for key, r in csv_rows.items():
        e = by_key[key]
        assert e["market"] == r["market"]
        assert e["instrument_type"] == r["instrument_type"]
        assert e["name"] == r["name"]
        assert e["sector_gics"] == r["sector_gics"]
        assert e["currency"] == r["currency"]
        assert e["economic_exposure"] == r["economic_exposure"].split("|")


def test_manifest_entries_carry_every_required_field():
    for e in _manifest():
        assert not [k for k in REQUIRED_FIELDS if k not in e]
        assert isinstance(e["economic_exposure"], list) and e["economic_exposure"]


def test_symbol_map_from_universe_manifest():
    """ADR-0007: the manifest is the canonical universe — every entry maps to
    a vendor code under the same strict rules as the seed CSV."""
    from pathlib import Path

    from atlas.dcp.market_data.adapters.eodhd import symbol_map_from_universe

    m = symbol_map_from_universe(Path(__file__).parents[2] / "seeds" / "universe.json")
    assert len(m) >= 112
    assert m["AAPL"] == "AAPL.US"
    assert m["BRK-B"] == "BRK-B.US"      # vendor code passes through verbatim
    assert m["NDIA"] == "NDIA.AU"        # ASX rule unchanged
    assert m["SPY"] == "SPY.US"
