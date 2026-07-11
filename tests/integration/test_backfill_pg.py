"""Backfill (task 1c): range-fetch bars + corporate actions, per-day gates, FX.

Golden ingestion regression on fixtures: the AVGO fixture week contains a 10:1
split (2024-07-15, close 1730 -> 172.50); the split must EXPLAIN the move so the
gate stays green — and a market with missing data must go honestly RED, never
green. All 8 US seed instruments carry fixture bars, so losing any one of them
is detectable (review finding: day-level gates masked instrument-level holes).
"""
from datetime import UTC, date, datetime
from decimal import Decimal
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
    # other tests (fx CLI) legitimately commit this fixture row; remove it so the
    # out-of-window assertion below is deterministic across runs
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE rate_date = '2026-07-10'"))


def _run(s, markets=("US",), seeds=SEEDS):
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 15, 22, tzinfo=UTC)))
    return backfill(session=s, adapter=FixtureAdapter(FIXTURES), audit=audit,
                    markets=list(markets), start=START, end=END, seeds_csv=seeds)


def test_backfill_us_fixture_week_zero_red_gates(clean_audit):
    s = clean_audit
    _clean_window(s)
    report = _run(s)
    us = report.markets["US"]
    assert us.sessions == 4                      # 10, 11, 12, 15 July 2024 (XNYS)
    assert us.red == 0                           # split-explained move stays green
    assert us.amber == 0
    assert us.bars == 32                         # 8 instruments x 4 sessions, complete
    assert not report.failed
    gates = s.execute(text(
        "SELECT gate_date, status FROM market.data_quality_gates "
        "WHERE market='US' AND gate_date BETWEEN :a AND :b ORDER BY gate_date"),
        {"a": START, "b": END}).all()
    assert [g.status for g in gates] == ["green"] * 4
    split = s.execute(text(
        "SELECT ratio FROM market.corporate_actions ca "
        "JOIN market.instruments i ON i.id = ca.instrument_id "
        "WHERE i.symbol='AVGO' AND ca.action_date='2024-07-15'")).scalar()
    assert split is not None and int(split) == 10


def test_backfill_writes_fx_series_exactly(clean_audit):
    """Range-pinned: exactly the 4 in-window rows, exact rates — a lost range
    filter (fetching all history) must fail this (review finding: mutation
    removing the filter previously passed the whole suite)."""
    s = clean_audit
    _clean_window(s)
    report = _run(s)
    assert report.fx["USDAUD"].rows == 4
    assert report.fx["USDAUD"].empty is False
    rows = s.execute(text(
        "SELECT rate_date, rate FROM market.fx_rates_daily "
        "WHERE base='USD' AND quote='AUD' AND rate_date BETWEEN :a AND :b "
        "ORDER BY rate_date"), {"a": START, "b": END}).all()
    assert [(r.rate_date.isoformat(), Decimal(r.rate)) for r in rows] == [
        ("2024-07-10", Decimal("1.4800")),
        ("2024-07-11", Decimal("1.4820")),
        ("2024-07-12", Decimal("1.4790")),
        ("2024-07-15", Decimal("1.4810")),
    ]
    # the 2026 fixture row must NOT have been written by a 2024 window
    out_of_window = s.execute(text(
        "SELECT count(*) FROM market.fx_rates_daily "
        "WHERE rate_date='2026-07-10' AND source='FixtureAdapter'")).scalar()
    assert out_of_window == 0


def test_backfill_is_idempotent_across_all_written_tables(clean_audit):
    """Counts bars AND corporate_actions AND gates AND fx — the original test
    only counted bars while corporate_actions silently duplicated (review
    finding; migration 0005 added the natural key)."""
    s = clean_audit
    _clean_window(s)
    _run(s)
    counts_sql = ("SELECT (SELECT count(*) FROM market.price_bars_daily),"
                  "(SELECT count(*) FROM market.corporate_actions),"
                  "(SELECT count(*) FROM market.data_quality_gates),"
                  "(SELECT count(*) FROM market.fx_rates_daily)")
    before = s.execute(text(counts_sql)).one()
    _run(s)
    after = s.execute(text(counts_sql)).one()
    assert tuple(before) == tuple(after)


def test_backfill_detects_missing_instrument(clean_audit, tmp_path):
    """An instrument with no vendor data must turn the market RED even when
    every other instrument is complete (review finding: one instrument's bars
    masked all others)."""
    s = clean_audit
    _clean_window(s)
    seeds = tmp_path / "seeds.csv"
    seeds.write_text(SEEDS.read_text().rstrip("\n")
                     + "\nGHOST,NYSE,US,stock,Ghost Corp,Broad,USD,US\n")
    report = _run(s, seeds=seeds)
    us = report.markets["US"]
    assert us.red == us.sessions
    assert report.failed
    reason = s.execute(text(
        "SELECT reasons FROM market.data_quality_gates "
        "WHERE market='US' AND gate_date='2024-07-15'")).scalar()
    assert "GHOST" in str(reason)
    # cleanup: deactivate the ghost so later tests' expected set is unaffected
    s.execute(text("DELETE FROM market.instruments WHERE symbol='GHOST'"))
    s.commit()


def test_backfill_market_without_data_reports_red_not_green(clean_audit):
    """A market with no vendor data must surface RED gates — the honest failure."""
    s = clean_audit
    _clean_window(s)
    report = _run(s, markets=("AU",))
    au = report.markets["AU"]
    assert au.sessions > 0
    assert au.red == au.sessions                 # every AU session is missing -> RED
    assert report.failed
    statuses = s.execute(text(
        "SELECT DISTINCT status FROM market.data_quality_gates "
        "WHERE market='AU' AND gate_date BETWEEN :a AND :b"),
        {"a": START, "b": END}).scalars().all()
    assert statuses == ["red"]


def test_backfill_emits_audit_event(clean_audit):
    s = clean_audit
    _clean_window(s)
    _run(s)
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type='market.backfill.completed'")).scalar()
    assert payload["markets"]["US"]["red"] == 0
    assert payload["fx"]["USDAUD"]["empty"] is False
