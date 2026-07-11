"""Backfill (task 1c): range-fetch bars + corporate actions, per-day gates, FX.

Golden ingestion regression on fixtures: the AVGO fixture week contains a 10:1
split (2024-07-15, close 1730 -> 172.50); the split must EXPLAIN the move so the
gate stays green — and a market with no data must go honestly RED, never green.
"""
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.backfill import backfill
from tests.conftest import requires_pg

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SEEDS = ROOT / "seeds" / "instruments_seed.csv"
START, END = date(2024, 7, 10), date(2024, 7, 15)


def _clean_window(s) -> None:
    s.execute(text("DELETE FROM market.data_quality_gates "
                   "WHERE gate_date BETWEEN :a AND :b"), {"a": START, "b": END})
    s.execute(text("DELETE FROM market.fx_rates_daily "
                   "WHERE rate_date BETWEEN :a AND :b"), {"a": START, "b": END})


def _run(s, markets=("US",)):
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 15, 22, tzinfo=UTC)))
    return backfill(session=s, adapter=FixtureAdapter(FIXTURES), audit=audit,
                    markets=list(markets), start=START, end=END, seeds_csv=SEEDS)


def test_backfill_us_fixture_week_zero_red_gates(clean_audit):
    s = clean_audit
    _clean_window(s)
    summary = _run(s)
    us = summary["US"]
    assert us.sessions == 4                      # 10, 11, 12, 15 July 2024 (XNYS)
    assert us.red == 0                           # split-explained move stays green
    assert us.amber == 0
    gates = s.execute(text(
        "SELECT gate_date, status FROM market.data_quality_gates "
        "WHERE market='US' AND gate_date BETWEEN :a AND :b ORDER BY gate_date"),
        {"a": START, "b": END}).all()
    assert [g.status for g in gates] == ["green"] * 4
    bars = s.execute(text(
        "SELECT count(*) FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol='AVGO' AND pb.bar_date BETWEEN :a AND :b"),
        {"a": START, "b": END}).scalar()
    assert bars == 4
    split = s.execute(text(
        "SELECT ratio FROM market.corporate_actions ca "
        "JOIN market.instruments i ON i.id = ca.instrument_id "
        "WHERE i.symbol='AVGO' AND ca.action_date='2024-07-15'")).scalar()
    assert split is not None and int(split) == 10


def test_backfill_writes_fx_series(clean_audit):
    s = clean_audit
    _clean_window(s)
    _run(s)
    n = s.execute(text(
        "SELECT count(*) FROM market.fx_rates_daily "
        "WHERE base='USD' AND quote='AUD' AND rate_date BETWEEN :a AND :b"),
        {"a": START, "b": END}).scalar()
    assert n >= 4


def test_backfill_is_idempotent(clean_audit):
    s = clean_audit
    _clean_window(s)
    _run(s)
    before = s.execute(text("SELECT count(*) FROM market.price_bars_daily")).scalar()
    _run(s)
    after = s.execute(text("SELECT count(*) FROM market.price_bars_daily")).scalar()
    assert before == after


def test_backfill_market_without_data_reports_red_not_green(clean_audit):
    """A market with no vendor data must surface RED gates — the honest failure."""
    s = clean_audit
    _clean_window(s)
    summary = _run(s, markets=("AU",))
    au = summary["AU"]
    assert au.sessions > 0
    assert au.red == au.sessions                 # every AU session is missing -> RED
    statuses = s.execute(text(
        "SELECT DISTINCT status FROM market.data_quality_gates "
        "WHERE market='AU' AND gate_date BETWEEN :a AND :b"),
        {"a": START, "b": END}).scalars().all()
    assert statuses == ["red"]


def test_backfill_emits_audit_event(clean_audit):
    s = clean_audit
    _clean_window(s)
    _run(s)
    n = s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type='market.backfill.completed'")).scalar()
    assert n == 1
