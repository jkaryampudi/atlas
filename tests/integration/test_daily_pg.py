"""Nightly incremental ingest: per-instrument windows from the latest stored
bar through the last COMPLETED session, gates from stored bars, carry-forward
on non-trading days, incremental FX with strict weekday reconciliation.

Base state per test: standard seeds with bars/gates/FX exactly through
Friday 2024-07-12 (leftovers from other tests scrubbed), all built inside the
test transaction and rolled back at teardown — nothing here can pollute the
per-instrument gate expectations of the rest of the suite. Only the CLI tests
commit (a fresh connection must see the data); they restore the canonical
complete fixture week afterwards, exactly the state the backfill tests build.
"""
from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.backfill import backfill
from atlas.dcp.market_data.daily import main as daily_main
from atlas.dcp.market_data.daily import run_daily_ingest
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SEEDS = ROOT / "seeds" / "instruments_seed.csv"
BASE_START, BASE_END = date(2024, 7, 10), date(2024, 7, 12)
MONDAY = date(2024, 7, 15)
# Tuesday 02:00 UTC = Monday 22:00 ET: Monday's session and FOREX day are done.
AFTER_MONDAY_CLOSE = datetime(2024, 7, 16, 2, 0, tzinfo=UTC)

COUNTS_SQL = ("SELECT (SELECT count(*) FROM market.price_bars_daily),"
              "(SELECT count(*) FROM market.corporate_actions),"
              "(SELECT count(*) FROM market.data_quality_gates),"
              "(SELECT count(*) FROM market.fx_rates_daily)")


def _base(s) -> None:
    """Bars/gates/FX exactly through BASE_END; nothing after (uncommitted)."""
    s.execute(text("DELETE FROM market.price_bars_daily WHERE bar_date > :d"),
              {"d": BASE_END})
    s.execute(text("DELETE FROM market.corporate_actions WHERE action_date > :d"),
              {"d": BASE_END})
    s.execute(text("DELETE FROM market.data_quality_gates WHERE gate_date > :d"),
              {"d": BASE_END})
    s.execute(text("DELETE FROM market.fx_rates_daily"))
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 12, 22, tzinfo=UTC)))
    backfill(session=s, adapter=FixtureAdapter(FIXTURES), audit=audit, markets=["US"],
             start=BASE_START, end=BASE_END, seeds_csv=SEEDS)


@pytest.fixture
def base_state(clean_audit):
    _base(clean_audit)
    yield clean_audit  # no commit anywhere: pg_session rollback isolates all of it


def test_daily_happy_path_ingests_only_the_new_session(base_state):
    s = base_state
    report = run_daily_ingest(s, FrozenClock(AFTER_MONDAY_CLOSE), FixtureAdapter(FIXTURES))
    us = report.markets["US"]
    assert us.days == (MONDAY,)
    assert us.bars == 8                          # 8 US instruments x 1 new session
    assert us.gates == ((MONDAY, "green"),)      # AVGO 10:1 split explains the move
    assert us.needs_backfill == ()
    # AU: NDIA has no history — reported, never silently deep-backfilled
    assert report.markets["AU"].needs_backfill == ("NDIA",)
    assert report.markets["AU"].bars == 0
    assert report.needs_backfill == ("AU:NDIA",)
    # FX: Monday only; the weekend is not a gap
    assert report.fx["USDAUD"].rows == 1
    assert report.fx["USDAUD"].missing_weekdays == ()
    assert report.failures == ()
    assert not report.failed
    n = s.execute(text(
        "SELECT count(*) FROM market.corporate_actions ca "
        "JOIN market.instruments i ON i.id = ca.instrument_id "
        "WHERE i.symbol='AVGO' AND ca.action_date=:d"), {"d": MONDAY}).scalar()
    assert n == 1
    gate = s.execute(text("SELECT status FROM market.data_quality_gates "
                          "WHERE market='US' AND gate_date=:d"), {"d": MONDAY}).scalar()
    assert gate == "green"


def test_daily_emits_one_audit_event_with_full_summary(base_state):
    s = base_state
    run_daily_ingest(s, FrozenClock(AFTER_MONDAY_CLOSE), FixtureAdapter(FIXTURES))
    rows = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type='market.daily_ingest.completed'")).scalars().all()
    assert len(rows) == 1
    payload = rows[0]
    assert payload["failed"] is False
    assert payload["markets"]["US"]["bars"] == 8
    assert payload["markets"]["US"]["gates"]["2024-07-15"] == "green"
    assert payload["markets"]["AU"]["needs_backfill"] == ["NDIA"]
    assert payload["fx"]["USDAUD"]["rows"] == 1


def test_daily_mid_session_never_stores_the_in_progress_day(base_state):
    s = base_state
    # Monday 15:00 UTC = 11:00 ET: XNYS is open; the fixture HAS a Monday bar,
    # but a partial session must never be requested or stored.
    report = run_daily_ingest(s, FrozenClock(datetime(2024, 7, 15, 15, 0, tzinfo=UTC)),
                              FixtureAdapter(FIXTURES), markets=("US",))
    us = report.markets["US"]
    assert us.days == () and us.bars == 0
    stored = s.execute(text("SELECT count(*) FROM market.price_bars_daily "
                            "WHERE bar_date=:d"), {"d": MONDAY}).scalar()
    assert stored == 0
    # the weekend still gets its honest carry-forward gates (Friday was green)
    assert us.gates == ((date(2024, 7, 13), "green"), (date(2024, 7, 14), "green"))
    reasons = s.execute(text("SELECT reasons FROM market.data_quality_gates "
                             "WHERE market='US' AND gate_date='2024-07-13'")).scalar()
    assert "non-trading day" in str(reasons)
    assert not report.failed


def test_daily_second_run_is_a_no_op(base_state):
    s = base_state
    clock = FrozenClock(AFTER_MONDAY_CLOSE)
    run_daily_ingest(s, clock, FixtureAdapter(FIXTURES))
    before = tuple(s.execute(text(COUNTS_SQL)).one())
    second = run_daily_ingest(s, clock, FixtureAdapter(FIXTURES))
    assert tuple(s.execute(text(COUNTS_SQL)).one()) == before
    assert second.markets["US"].days == ()
    assert second.markets["US"].bars == 0
    assert second.markets["US"].gates == ()
    assert second.fx["USDAUD"].rows == 0
    assert not second.failed


def test_daily_missing_instrument_day_goes_red(base_state, tmp_path):
    """A vendor hole for ONE instrument must red the day even though the other
    seven are complete (per-instrument coverage, rules v1.1)."""
    s = base_state
    root = tmp_path / "fixtures"
    (root / "bars").mkdir(parents=True)
    for f in (FIXTURES / "bars").glob("*.csv"):
        lines = f.read_text().splitlines()
        if f.stem == "MSFT":
            lines = [ln for ln in lines if not ln.startswith(MONDAY.isoformat())]
        (root / "bars" / f.name).write_text("\n".join(lines) + "\n")
    for name in ("fx.csv", "splits.csv"):
        (root / name).write_text((FIXTURES / name).read_text())

    report = run_daily_ingest(s, FrozenClock(AFTER_MONDAY_CLOSE), FixtureAdapter(root),
                              markets=("US",))
    us = report.markets["US"]
    assert us.bars == 7
    assert us.gates == ((MONDAY, "red"),)
    assert report.failed
    reasons = s.execute(text("SELECT reasons FROM market.data_quality_gates "
                             "WHERE market='US' AND gate_date=:d"), {"d": MONDAY}).scalar()
    assert "MSFT" in str(reasons)


def test_daily_bare_instrument_reports_needs_backfill_and_reds_the_gate(base_state):
    """No stored bars at all -> needs_backfill, never a silent deep-backfill —
    and the gate goes honestly RED until the deliberate backfill happens."""
    s = base_state
    s.execute(text("INSERT INTO market.instruments (symbol, exchange, market, "
                   "instrument_type, name, currency) VALUES "
                   "('TDLY','NYSE','US','stock','Test Daily Corp','USD') "
                   "ON CONFLICT (symbol, exchange) DO NOTHING"))
    report = run_daily_ingest(s, FrozenClock(AFTER_MONDAY_CLOSE), FixtureAdapter(FIXTURES),
                              markets=("US",))
    us = report.markets["US"]
    assert us.needs_backfill == ("TDLY",)
    assert us.bars == 8                          # the other instruments still advance
    assert us.gates == ((MONDAY, "red"),)
    assert report.failed
    reasons = s.execute(text("SELECT reasons FROM market.data_quality_gates "
                             "WHERE market='US' AND gate_date=:d"), {"d": MONDAY}).scalar()
    assert "TDLY" in str(reasons)


def test_daily_fx_weekend_gap_is_fine(base_state):
    s = base_state
    report = run_daily_ingest(s, FrozenClock(datetime(2024, 7, 15, 23, 0, tzinfo=UTC)),
                              FixtureAdapter(FIXTURES), markets=())
    assert report.fx["USDAUD"].rows == 1         # Monday; Sat/Sun are not weekdays
    assert report.fx["USDAUD"].missing_weekdays == ()
    assert not report.failed


def test_daily_fx_weekday_gap_is_failure(base_state):
    s = base_state
    # Window runs through Wednesday 07-17; the fixture stops at Monday 07-15.
    report = run_daily_ingest(s, FrozenClock(datetime(2024, 7, 17, 23, 0, tzinfo=UTC)),
                              FixtureAdapter(FIXTURES), markets=())
    f = report.fx["USDAUD"]
    assert f.rows == 1
    assert f.missing_weekdays == (date(2024, 7, 16), date(2024, 7, 17))
    assert report.failed


def test_daily_fx_required_pair_without_history_is_failure(base_state):
    s = base_state
    s.execute(text("DELETE FROM market.fx_rates_daily"))
    report = run_daily_ingest(s, FrozenClock(AFTER_MONDAY_CLOSE), FixtureAdapter(FIXTURES),
                              markets=())
    assert any("USDAUD" in msg and "no stored rates" in msg for msg in report.failures)
    assert report.failed


@pytest.fixture
def cli_env(monkeypatch, pg_session):
    """Point session_scope at atlas_test and force the fixture adapter."""
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    monkeypatch.setenv("ATLAS_EODHD_API_KEY", "")  # never hit the real vendor here
    reset_app_engine()
    yield monkeypatch, pg_session
    reset_app_engine()


def _committed_full_week(s) -> None:
    """Canonical committed state (same as the backfill tests build): the full
    fixture week through 2024-07-15, green gates, FX series."""
    s.execute(text("DELETE FROM market.data_quality_gates "
                   "WHERE gate_date BETWEEN :a AND :b"), {"a": BASE_START, "b": MONDAY})
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE rate_date > :d"), {"d": MONDAY})
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 15, 22, tzinfo=UTC)))
    backfill(session=s, adapter=FixtureAdapter(FIXTURES), audit=audit, markets=["US"],
             start=BASE_START, end=MONDAY, seeds_csv=SEEDS)
    s.commit()


def test_daily_cli_up_to_date_exits_zero(cli_env, capsys):
    monkeypatch, s = cli_env
    _committed_full_week(s)
    monkeypatch.setattr(sys, "argv", ["daily", "--market", "US",
                                      "--now", "2024-07-16T02:00:00+00:00"])
    with pytest.raises(SystemExit) as e:
        daily_main()
    assert e.value.code == 0
    assert "all green" in capsys.readouterr().out


def test_daily_cli_missing_vendor_days_exit_two(cli_env, capsys):
    monkeypatch, s = cli_env
    _committed_full_week(s)
    # Wednesday 23:00 UTC: sessions 07-16 and 07-17 are due but the fixture
    # vendor has nothing -> red gates -> alertable non-zero exit.
    monkeypatch.setattr(sys, "argv", ["daily", "--market", "US",
                                      "--now", "2024-07-17T23:00:00+00:00"])
    with pytest.raises(SystemExit) as e:
        daily_main()
    assert e.value.code == 2
    assert "FAILURES PRESENT" in capsys.readouterr().out
    # cleanup: remove the (honest) red gates this CLI run committed so the
    # rest of the suite starts from the canonical green week
    s.execute(text("DELETE FROM market.data_quality_gates "
                   "WHERE market='US' AND gate_date IN ('2024-07-16','2024-07-17')"))
    s.commit()
