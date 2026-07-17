"""Nightly estimate-snapshot step (daily ingest step 5, the ADR-0011 forward
archive): daily cadence gated by the once-daily guard, per-instrument
fail-soft that cannot touch bars/FX/fundamentals/earnings, ADR-0007 US
single-name population, and the counts in the daily-ingest audit payload.

Same conventions as the earnings-daily suite: state built inside the test
transaction (pg_session rollback isolates it), markets=() keeps bars/gates
out of the way, clock pinned to Tuesday 2024-07-16 02:00 UTC."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
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
# the ADR-0007 forward-archive population among the seeds: US stock/adr only
US_SINGLE_NAMES = ("AVGO", "HDB", "IBN", "INFY", "MSFT")

TREND = {"2024-06-30": {"earningsEstimateAvg": "11.6000",
                        "earningsEstimateNumberOfAnalysts": "28",
                        "epsTrendCurrent": "11.6000",
                        "epsRevisionsUpLast7days": "2"},
         "2024-09-30": {"earningsEstimateAvg": "12.1000",
                        "epsTrendCurrent": "12.1000"}}


class _TrendVendor(FixtureAdapter):
    """Fixture docs, plus an injected Earnings.Trend for AVGO (the shared
    fixture files stay untouched) and a vendor-call counter."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fundamentals_calls: list[str] = []

    def fetch_fundamentals(self, symbol: str) -> dict:
        self.fundamentals_calls.append(symbol)
        doc = super().fetch_fundamentals(symbol)
        if symbol == "AVGO":
            doc.setdefault("Earnings", {})["Trend"] = TREND
        return doc


class _PoisonedVendor(_TrendVendor):
    """The vendor melts for MSFT's fundamentals document."""

    def fetch_fundamentals(self, symbol: str) -> dict:
        if symbol == "MSFT":
            self.fundamentals_calls.append(symbol)
            raise RuntimeError("vendor 502")
        return super().fetch_fundamentals(symbol)


@pytest.fixture
def base_state(clean_audit):
    s = clean_audit
    seed_instruments(s, SEEDS)  # no-op when already seeded by earlier tests
    s.execute(text("DELETE FROM market.estimate_snapshots"))
    s.execute(text("DELETE FROM market.fundamentals"))
    s.execute(text("DELETE FROM market.earnings_calendar"))
    # keep FX quiet: rate present through the last completed FOREX weekday
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD', 'AUD', '2024-07-15', 1.4810, 'test') "
        "ON CONFLICT (base, quote, rate_date) DO NOTHING"))
    yield s


def _run(s, adapter=None):
    return run_daily_ingest(s, FrozenClock(NOW),
                            adapter or _TrendVendor(FIXTURES), markets=())


ROWS = ("SELECT es.fiscal_period_end, es.snapshot_date, es.eps_estimate_avg, "
        "       es.fetched_at, es.source "
        "FROM market.estimate_snapshots es "
        "JOIN market.instruments i ON i.id = es.instrument_id "
        "WHERE i.symbol = :sym ORDER BY es.fiscal_period_end")


def test_nightly_step_snapshots_us_single_names_and_audits_counts(base_state):
    s = base_state
    report = _run(s)
    est = report.estimates
    # population = ACTIVE US stock/adr; ETFs (SPY/QQQ/INDA/NDIA) are outside
    assert est.fetched == ("AVGO",)
    assert est.empty == ("HDB", "IBN", "INFY", "MSFT")
    assert est.failed == () and not est.skipped and est.stored == 2
    assert not any(msg.startswith("estimates") for msg in report.failures)

    avgo = s.execute(text(ROWS), {"sym": "AVGO"}).all()
    assert [(r.fiscal_period_end, r.snapshot_date) for r in avgo] == [
        (date(2024, 6, 30), TODAY), (date(2024, 9, 30), TODAY)]
    assert avgo[0].eps_estimate_avg == Decimal("11.6000")
    assert all(r.fetched_at == NOW and r.source == "_TrendVendor" for r in avgo)
    # nothing snapshot for instruments outside the population
    for sym in ("SPY", "QQQ", "INDA", "NDIA"):
        assert s.execute(text(ROWS), {"sym": sym}).all() == []

    audit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.daily_ingest.completed'")).scalar()
    assert audit["estimates"] == {"skipped": False, "fetched": ["AVGO"],
                                  "empty": ["HDB", "IBN", "INFY", "MSFT"],
                                  "failed": [], "rows_stored": 2}


def test_cycle_rerun_same_session_is_guarded_and_spends_no_vendor_calls(base_state):
    s = base_state
    _run(s)
    before = s.execute(text("SELECT count(*) FROM market.estimate_snapshots")).scalar()
    adapter = _TrendVendor(FIXTURES)
    second = _run(s, adapter=adapter)
    est = second.estimates
    assert est.skipped                       # the once-daily guard fired
    assert est.fetched == () and est.empty == () and est.stored == 0
    # zero fundamentals calls in the whole re-run: the fundamentals step is
    # fresh (same-day snapshot) and the guarded estimates step never fetches
    assert adapter.fundamentals_calls == []
    assert s.execute(text(
        "SELECT count(*) FROM market.estimate_snapshots")).scalar() == before
    audit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.daily_ingest.completed' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert audit["estimates"]["skipped"] is True
    assert audit["estimates"]["rows_stored"] == 0


def test_estimates_vendor_failure_is_fail_soft_and_isolated(base_state):
    s = base_state
    report = _run(s, adapter=_PoisonedVendor(FIXTURES))
    est = report.estimates
    assert est.failed == ("MSFT",)
    assert est.fetched == ("AVGO",)          # the rest still snapshot
    assert est.empty == ("HDB", "IBN", "INFY")
    assert any(msg == "estimates MSFT: vendor fetch failed: vendor 502"
               for msg in report.failures)
    assert report.failed                     # alertable, exit 2 path
    # the failure is ISOLATED: earnings refreshed everyone, FX stayed quiet
    assert report.earnings.failed == ()
    assert len(report.earnings.fetched) == 9
    assert not any(f.missing_weekdays for f in report.fx.values())
    assert s.execute(text(ROWS), {"sym": "MSFT"}).all() == []  # nothing fabricated
    audit = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.daily_ingest.completed'")).scalar()
    assert audit["estimates"]["failed"] == ["MSFT"]
    assert audit["failed"] is True
