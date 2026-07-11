"""The universe manifest (seeds/universe.json) is the reviewed source of truth
for "what do we trade" — it must stay in lockstep with the instrument seed CSV
(no invented tickers, no drifted descriptive fields)."""
import csv
import json
from pathlib import Path

from atlas.dcp.market_data.universe import REQUIRED_FIELDS

ROOT = Path(__file__).parents[2]


def _manifest() -> list[dict[str, object]]:
    entries = json.loads((ROOT / "seeds" / "universe.json").read_text())
    assert isinstance(entries, list)
    return entries


def test_manifest_matches_instrument_seed_csv_exactly():
    with (ROOT / "seeds" / "instruments_seed.csv").open() as f:
        csv_rows = {(r["symbol"], r["exchange"]): r for r in csv.DictReader(f)}
    entries = _manifest()
    assert {(e["symbol"], e["exchange"]) for e in entries} == set(csv_rows)
    for e in entries:
        r = csv_rows[(str(e["symbol"]), str(e["exchange"]))]
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
