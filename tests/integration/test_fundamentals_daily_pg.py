"""Nightly fundamentals refresh (daily ingest step 3): staleness windows,
append-only snapshots, per-instrument fail-soft, audit payload.

All state is built inside the test transaction (pg_session rollback isolates
it); markets=() keeps the bar/gate machinery out of the way — the refresh
runs for every ACTIVE instrument regardless of market, like FX. Clock is
Tuesday 02:00 UTC -> the refresh's as_of (UTC date) is 2024-07-16."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.daily import run_daily_ingest
from atlas.dcp.market_data.ingest import seed_instruments
from tests.conftest import requires_pg

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SEEDS = ROOT / "seeds" / "instruments_seed.csv"
NOW = datetime(2024, 7, 16, 2, 0, tzinfo=UTC)
TODAY = date(2024, 7, 16)
ALL_SYMBOLS = ("AVGO", "HDB", "IBN", "INDA", "INFY", "MSFT", "NDIA", "QQQ", "SPY")

COUNT = "SELECT count(*) FROM market.fundamentals"


@pytest.fixture
def base_state(clean_audit):
    s = clean_audit
    seed_instruments(s, SEEDS)  # no-op when already seeded by earlier tests
    s.execute(text("DELETE FROM market.fundamentals"))
    # keep FX quiet: rate present through the last completed FOREX weekday
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2024-07-15', 1.4810, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO NOTHING"))
    yield s


def _snapshot(s, symbol: str, as_of: date, payload: dict | None = None) -> None:
    s.execute(text(
        "INSERT INTO market.fundamentals (instrument_id, as_of, payload, source) "
        "SELECT i.id, :d, CAST(:p AS jsonb), 'test' FROM market.instruments i "
        "WHERE i.symbol = :sym"),
        {"d": as_of, "p": json.dumps(payload or {"marker": True}), "sym": symbol})


def _run(s, adapter=None):
    return run_daily_ingest(s, FrozenClock(NOW),
                            adapter or FixtureAdapter(FIXTURES), markets=())


def test_absent_snapshots_are_fetched_and_stored_whole(base_state):
    s = base_state
    report = _run(s)
    f = report.fundamentals
    assert f.fetched == ALL_SYMBOLS
    assert f.fresh == ()
    assert f.failed == ()
    assert not any(msg.startswith("fundamentals") for msg in report.failures)
    rows = s.execute(text(
        "SELECT i.symbol, fu.as_of, fu.source, fu.payload FROM market.fundamentals fu "
        "JOIN market.instruments i ON i.id = fu.instrument_id "
        "ORDER BY i.symbol")).all()
    assert [r.symbol for r in rows] == list(ALL_SYMBOLS)
    assert all(r.as_of == TODAY and r.source == "FixtureAdapter" for r in rows)
    # the RAW vendor document is stored whole — free text and all (the
    # whitelist applies at extraction, not at storage)
    avgo = rows[0].payload
    assert avgo["Highlights"]["MarketCapitalization"] == 1252470423552
    assert "SYSTEM OVERRIDE" in avgo["General"]["Description"]
    # the run's summary lands in the audit event payload
    audit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.daily_ingest.completed'")).scalar()
    assert audit["fundamentals"] == {"fetched": list(ALL_SYMBOLS),
                                     "fresh": [], "failed": []}


def test_fresh_snapshot_is_skipped_seven_day_boundary(base_state):
    s = base_state
    for sym in ALL_SYMBOLS:
        _snapshot(s, sym, date(2024, 7, 9))  # exactly 7 days old: still fresh
    report = _run(s)
    assert report.fundamentals.fresh == ALL_SYMBOLS
    assert report.fundamentals.fetched == ()
    assert s.execute(text(COUNT)).scalar() == len(ALL_SYMBOLS)  # nothing added


def test_eight_day_old_snapshot_is_refetched_append_only(base_state):
    s = base_state
    _snapshot(s, "AVGO", date(2024, 7, 8))  # 8 days old: stale
    report = _run(s)
    assert "AVGO" in report.fundamentals.fetched   # stale -> refetched
    assert report.fundamentals.fresh == ()
    avgo = s.execute(text(
        "SELECT fu.as_of, fu.payload FROM market.fundamentals fu "
        "JOIN market.instruments i ON i.id = fu.instrument_id "
        "WHERE i.symbol = 'AVGO' ORDER BY fu.as_of")).all()
    # append-style: the old snapshot is UNTOUCHED, the refresh is a NEW row
    assert [r.as_of for r in avgo] == [date(2024, 7, 8), TODAY]
    assert avgo[0].payload == {"marker": True}
    assert avgo[1].payload["General"]["Code"] == "AVGO"


def test_same_day_second_run_is_a_no_op(base_state):
    s = base_state
    _run(s)
    before = s.execute(text(COUNT)).scalar()
    second = _run(s)
    assert second.fundamentals.fetched == ()
    assert second.fundamentals.fresh == ALL_SYMBOLS
    assert s.execute(text(COUNT)).scalar() == before


class _PoisonedVendor(FixtureAdapter):
    """Delegates to fixtures, but the vendor melts for one symbol."""

    def fetch_fundamentals(self, symbol: str) -> dict[str, object]:
        if symbol == "MSFT":
            raise RuntimeError("vendor 502")
        return super().fetch_fundamentals(symbol)


def test_vendor_failure_is_fail_soft_per_instrument(base_state):
    s = base_state
    report = _run(s, adapter=_PoisonedVendor(FIXTURES))
    f = report.fundamentals
    assert f.failed == ("MSFT",)
    assert "MSFT" not in f.fetched
    assert len(f.fetched) == len(ALL_SYMBOLS) - 1        # the rest still refresh
    assert any(msg == "fundamentals MSFT: vendor fetch failed: vendor 502"
               for msg in report.failures)
    assert report.failed                                  # alertable, exit 2 path
    n = s.execute(text(
        "SELECT count(*) FROM market.fundamentals fu "
        "JOIN market.instruments i ON i.id = fu.instrument_id "
        "WHERE i.symbol = 'MSFT'")).scalar()
    assert n == 0                                         # no fabricated snapshot
    audit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.daily_ingest.completed'")).scalar()
    assert audit["fundamentals"]["failed"] == ["MSFT"]
    assert audit["failed"] is True
