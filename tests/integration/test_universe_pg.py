"""Universe manifest sync: upsert from seeds/universe.json, never delete —
"what do we trade" is a reviewed file in git, not hand-run SQL. All mutations
stay inside the test transaction (no commits), so the suite's per-instrument
gate expectations are untouched at teardown.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.dcp.market_data.ingest import seed_instruments
from atlas.dcp.market_data.universe import main as universe_main
from atlas.dcp.market_data.universe import sync_universe
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "seeds" / "universe.json"


def test_sync_is_a_no_op_over_freshly_seeded_instruments(pg_session):
    # the SMALL fixture (the nine originals): the production manifest now
    # carries the full ADR-0007 universe and would legitimately insert 103
    s = pg_session
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    res = sync_universe(s, ROOT / "tests" / "fixtures" / "universe_small.json")
    assert res.inserted == ()
    assert res.updated == ()
    assert res.unchanged == 9


def test_sync_inserts_new_instrument_and_deletes_nothing(pg_session, tmp_path):
    s = pg_session
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    before = s.execute(text("SELECT count(*) FROM market.instruments")).scalar()
    manifest = tmp_path / "universe.json"
    manifest.write_text(json.dumps([{
        "symbol": "TUNIV", "exchange": "NYSE", "market": "US",
        "instrument_type": "stock", "name": "Test Universe Corp",
        "sector_gics": "Industrials", "currency": "USD",
        "economic_exposure": ["US"]}]))
    res = sync_universe(s, manifest)
    assert res.inserted == ("TUNIV@NYSE",)
    # a one-entry manifest must never delete the instruments it omits
    after = s.execute(text("SELECT count(*) FROM market.instruments")).scalar()
    assert after == before + 1
    row = s.execute(text("SELECT is_active, economic_exposure FROM market.instruments "
                         "WHERE symbol='TUNIV'")).one()
    assert row.is_active is True                # active by default; no bars yet ->
    assert list(row.economic_exposure) == ["US"]  # the nightly reports needs_backfill
    # idempotent: the second sync changes nothing
    res2 = sync_universe(s, manifest)
    assert res2.inserted == () and res2.updated == () and res2.unchanged == 1


def test_sync_updates_descriptive_fields_only(pg_session):
    s = pg_session
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    s.execute(text("UPDATE market.instruments SET name='Drifted Name' "
                   "WHERE symbol='SPY' AND exchange='NYSEARCA'"))
    res = sync_universe(s, MANIFEST)
    assert res.updated == ("SPY@NYSEARCA",)
    assert res.unchanged == 8
    name = s.execute(text("SELECT name FROM market.instruments "
                          "WHERE symbol='SPY' AND exchange='NYSEARCA'")).scalar()
    assert name == "SPDR S&P 500 ETF"           # manifest is the source of truth


def test_sync_never_reactivates_a_deactivated_instrument(pg_session):
    """Deactivation is a human decision; a manifest sync must not undo it."""
    s = pg_session
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    s.execute(text("UPDATE market.instruments SET is_active=false "
                   "WHERE symbol='SPY' AND exchange='NYSEARCA'"))
    sync_universe(s, MANIFEST)
    active = s.execute(text("SELECT is_active FROM market.instruments "
                            "WHERE symbol='SPY' AND exchange='NYSEARCA'")).scalar()
    assert active is False


def test_sync_rejects_incomplete_entries(pg_session, tmp_path):
    manifest = tmp_path / "universe.json"
    manifest.write_text(json.dumps([{"symbol": "BAD", "exchange": "NYSE"}]))
    with pytest.raises(ValueError, match="missing fields"):
        sync_universe(pg_session, manifest)


def test_sync_rejects_non_array_manifest(pg_session, tmp_path):
    manifest = tmp_path / "universe.json"
    manifest.write_text(json.dumps({"symbol": "BAD"}))
    with pytest.raises(ValueError, match="JSON array"):
        sync_universe(pg_session, manifest)


def test_universe_cli_syncs_and_audits(monkeypatch, pg_session, capsys):
    """The CLI is how the manifest actually lands: it must report the outcome
    and leave an audit trail (universe changes are material actions)."""
    s = pg_session
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    s.commit()  # the CLI opens its own connection and must see the seeds
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    # a SMALL fixture manifest: the real one now carries the full ADR-0007
    # universe, and committing 103 barless instruments into atlas_test would
    # red-gate every later test (observed) — the CLI contract is what is
    # under test, not the production universe
    small = ROOT / "tests" / "fixtures" / "universe_small.json"
    monkeypatch.setattr(sys, "argv", ["universe", "--path", str(small)])
    universe_main()  # no SystemExit on success (fx.py convention)
    reset_app_engine()
    assert "unchanged=9" in capsys.readouterr().out
    n = s.execute(text("SELECT count(*) FROM audit.decision_events "
                       "WHERE event_type='market.universe.synced' "
                       "AND actor_type='human'")).scalar()
    assert n >= 1
