"""Nightly earnings-calendar refresh (daily ingest step 4): staleness window,
supersede-on-refresh, closed when_time vocabulary at rest, per-instrument
fail-soft, and the counts in the report + audit payload.

Same conventions as the fundamentals suite: state built inside the test
transaction (pg_session rollback isolates it), markets=() keeps bars/gates
out of the way, clock pinned to Tuesday 2024-07-16 02:00 UTC — so today is
2024-07-16 and the fetch window is [2023-12-29, 2024-08-15]."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.daily import run_daily_ingest
from atlas.dcp.market_data.earnings import STALE_DAYS
from atlas.dcp.market_data.ingest import seed_instruments
from tests.conftest import requires_pg

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SEEDS = ROOT / "seeds" / "instruments_seed.csv"
NOW = datetime(2024, 7, 16, 2, 0, tzinfo=UTC)
TODAY = date(2024, 7, 16)
ALL_SYMBOLS = ("AVGO", "HDB", "IBN", "INDA", "INFY", "MSFT", "NDIA", "QQQ", "SPY")

ROWS = ("SELECT ec.report_date, ec.when_time, ec.fetched_at, ec.source "
        "FROM market.earnings_calendar ec "
        "JOIN market.instruments i ON i.id = ec.instrument_id "
        "WHERE i.symbol = :sym ORDER BY ec.report_date")


@pytest.fixture
def base_state(clean_audit):
    s = clean_audit
    seed_instruments(s, SEEDS)  # no-op when already seeded by earlier tests
    s.execute(text("DELETE FROM market.earnings_calendar"))
    # keep FX quiet: rate present through the last completed FOREX weekday
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2024-07-15', 1.4810, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO NOTHING"))
    yield s


def _seed_row(s, symbol: str, report_date: date, fetched_at: datetime,
              when_time: str | None = None) -> None:
    s.execute(text(
        "INSERT INTO market.earnings_calendar "
        "(instrument_id, report_date, when_time, fetched_at, source) "
        "SELECT i.id, :d, :w, :fa, 'test' FROM market.instruments i "
        "WHERE i.symbol = :sym"),
        {"d": report_date, "w": when_time, "fa": fetched_at, "sym": symbol})


def _run(s, adapter=None):
    return run_daily_ingest(s, FrozenClock(NOW),
                            adapter or FixtureAdapter(FIXTURES), markets=())


def test_absent_calendars_are_fetched_windowed_and_flag_whitelisted(base_state):
    s = base_state
    report = _run(s)
    e = report.earnings
    assert e.fetched == ALL_SYMBOLS      # nothing stored -> every instrument checked
    assert e.fresh == () and e.failed == ()
    assert not any(msg.startswith("earnings") for msg in report.failures)
    # AVGO: past + future rows inside [today-200, today+30]; the 2024-09-05
    # row lies beyond the +30 window and is not stored
    avgo = s.execute(text(ROWS), {"sym": "AVGO"}).all()
    assert [(r.report_date, r.when_time) for r in avgo] == [
        (date(2024, 6, 12), "AfterMarket"), (date(2024, 7, 25), "AfterMarket")]
    assert all(r.fetched_at == NOW and r.source == "FixtureAdapter" for r in avgo)
    # INFY's hostile vendor flag was normalized at the adapter boundary:
    # stored as NULL, the free text nowhere in the database
    infy = s.execute(text(ROWS), {"sym": "INFY"}).all()
    assert [(r.report_date, r.when_time) for r in infy] == [(date(2024, 7, 18), None)]
    # ETFs: the vendor has nothing — checked, zero rows, no failure
    assert s.execute(text(ROWS), {"sym": "SPY"}).all() == []
    # the run's summary lands in the audit event payload
    audit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.daily_ingest.completed'")).scalar()
    assert audit["earnings"] == {"fetched": list(ALL_SYMBOLS),
                                 "fresh": [], "failed": []}


def test_staleness_boundary_three_days_fresh_four_days_stale(base_state):
    s = base_state
    for sym in ALL_SYMBOLS:
        _seed_row(s, sym, date(2024, 4, 2), NOW - timedelta(days=STALE_DAYS))
    report = _run(s)   # newest fetched_at exactly STALE_DAYS old: still fresh
    assert report.earnings.fresh == ALL_SYMBOLS
    assert report.earnings.fetched == ()
    s.execute(text("DELETE FROM market.earnings_calendar"))
    for sym in ALL_SYMBOLS:
        _seed_row(s, sym, date(2024, 4, 2), NOW - timedelta(days=STALE_DAYS + 1))
    report = _run(s)   # one day older: stale, refetched
    assert report.earnings.fetched == ALL_SYMBOLS
    assert report.earnings.fresh == ()


def test_same_day_second_run_is_a_data_no_op(base_state):
    """Instruments that got rows are fresh and skipped; instruments the vendor
    had NOTHING for have no fetched_at to throttle on and are re-checked
    (documented in earnings.py — a handful of cheap calls, honestly spent
    rather than a fabricated marker row). Either way: zero new rows."""
    s = base_state
    _run(s)
    before = s.execute(text("SELECT count(*) FROM market.earnings_calendar")).scalar()
    second = _run(s)
    assert second.earnings.fresh == ("AVGO", "HDB", "INFY", "MSFT")   # have rows
    assert second.earnings.fetched == ("IBN", "INDA", "NDIA", "QQQ", "SPY")
    assert s.execute(text(
        "SELECT count(*) FROM market.earnings_calendar")).scalar() == before


def test_refresh_supersedes_moved_future_dates_but_never_past_facts(base_state):
    s = base_state
    stale = NOW - timedelta(days=10)
    # the vendor previously scheduled AVGO for 2024-07-20; it has since moved
    # to 2024-07-25 (the fixture). The past 2024-05-01 row is a recorded fact.
    _seed_row(s, "AVGO", date(2024, 7, 20), stale, when_time="AfterMarket")
    _seed_row(s, "AVGO", date(2024, 5, 1), stale)
    report = _run(s)
    assert "AVGO" in report.earnings.fetched
    avgo = s.execute(text(ROWS), {"sym": "AVGO"}).all()
    assert [r.report_date for r in avgo] == [
        date(2024, 5, 1),    # past fact: kept, even though the vendor omits it
        date(2024, 6, 12),   # vendor past row upserted
        date(2024, 7, 25),   # the rescheduled print
    ]                        # 2024-07-20 phantom: DELETED
    # ETFs with a stale past marker: empty vendor window deletes nothing past
    _seed_row(s, "SPY", date(2024, 3, 1), stale)
    report = _run(s)
    assert s.execute(text(ROWS), {"sym": "SPY"}).all() != []


def test_refetch_updates_fetched_at_on_existing_rows(base_state):
    s = base_state
    stale = NOW - timedelta(days=10)
    _seed_row(s, "AVGO", date(2024, 6, 12), stale, when_time=None)
    _run(s)
    row = s.execute(text(ROWS), {"sym": "AVGO"}).all()[0]
    # natural-key upsert refreshed the vendor's current view of the same date
    assert row.report_date == date(2024, 6, 12)
    assert row.when_time == "AfterMarket" and row.fetched_at == NOW


class _PoisonedVendor(FixtureAdapter):
    """Delegates to fixtures, but the vendor melts for one symbol."""

    def fetch_earnings_calendar(self, symbol, start, end):
        if symbol == "MSFT":
            raise RuntimeError("vendor 502")
        return super().fetch_earnings_calendar(symbol, start, end)


def test_vendor_failure_is_fail_soft_per_instrument(base_state):
    s = base_state
    report = _run(s, adapter=_PoisonedVendor(FIXTURES))
    e = report.earnings
    assert e.failed == ("MSFT",)
    assert "MSFT" not in e.fetched
    assert len(e.fetched) == len(ALL_SYMBOLS) - 1        # the rest still refresh
    assert any(msg == "earnings MSFT: vendor fetch failed: vendor 502"
               for msg in report.failures)
    assert report.failed                                  # alertable, exit 2 path
    assert s.execute(text(ROWS), {"sym": "MSFT"}).all() == []  # nothing fabricated
    audit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.daily_ingest.completed'")).scalar()
    assert audit["earnings"]["failed"] == ["MSFT"]
    assert audit["failed"] is True
