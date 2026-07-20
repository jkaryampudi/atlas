import csv
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.ingest import ingest_day, seed_instruments, write_gate
from atlas.dcp.market_data.models import GateStatus
from atlas.dcp.market_data.quality import GateResult
from atlas.dcp.risk.seed_limits import seed_limit_set
from tests.conftest import URL, requires_pg

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]

_SEED_SYMBOLS = sorted({r["symbol"] for r in csv.DictReader(
    open(ROOT / "seeds" / "instruments_seed.csv"))})


def _scrub_committed_seed_world() -> None:
    """These tests COMMIT the seed world (s.commit() — ingest_day's gates and
    the audit chain are asserted through the same session). Committed ACTIVE
    instruments leak into every later test that resolves by symbol: a second
    active 'SPY' made compute_models' relative-strength pick a coin-flip (the
    intermittent test_rising_stock_reads_bullish failure), exactly the ATLZ
    lesson. This module therefore scrubs its own committed world — the seed
    CSV's symbols and their bars/actions/gates — before and after."""
    engine = create_engine(URL)
    try:
        with engine.begin() as c:
            c.execute(text(
                "DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                "(SELECT id FROM market.instruments WHERE symbol = ANY(:s))"),
                {"s": _SEED_SYMBOLS})
            c.execute(text(
                "DELETE FROM market.corporate_actions WHERE instrument_id IN "
                "(SELECT id FROM market.instruments WHERE symbol = ANY(:s))"),
                {"s": _SEED_SYMBOLS})
            c.execute(text("DELETE FROM market.fx_rates_daily"))
            c.execute(text("DELETE FROM market.data_quality_gates"))
            c.execute(text("DELETE FROM market.instruments WHERE symbol = ANY(:s)"),
                      {"s": _SEED_SYMBOLS})
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _committed_world_isolation(pg_session):
    # depends on pg_session so the test database exists before the scrub
    _scrub_committed_seed_world()
    yield
    _scrub_committed_seed_world()


def test_full_ingestion_day_from_fixtures(clean_audit):
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    seed_limit_set(s, ROOT / "seeds" / "limit_set_v1.json")
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 15, 22, 0, tzinfo=UTC)))
    status = ingest_day(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                        audit=audit, market="US", day=date(2024, 7, 15),
                        lookback_sessions=0)
    s.commit()
    assert status is GateStatus.GREEN
    bars = s.execute(text("SELECT count(*) FROM market.price_bars_daily")).scalar()
    assert bars >= 1
    gate = s.execute(text("SELECT status FROM market.data_quality_gates "
                          "WHERE market='US' AND gate_date='2024-07-15'")).scalar()
    assert gate == "green"
    lim = s.execute(text("SELECT mode FROM risk.limit_sets WHERE version=1")).scalar()
    assert lim == "small_aum"
    assert audit.verify() >= 1


def test_missing_day_produces_red_gate(clean_audit):
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 16, 22, 0, tzinfo=UTC)))
    status = ingest_day(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                        audit=audit, market="US", day=date(2024, 7, 16),  # no fixture data
                        lookback_sessions=0)
    assert status is GateStatus.RED


def test_split_day_with_lookback_is_green(clean_audit):
    """Calendar-aware lookback crosses the weekend to Friday; the 10:1 split
    explains the 90% move, so the sanity check must not fire."""
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 15, 22, 0, tzinfo=UTC)))
    status = ingest_day(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                        audit=audit, market="US", day=date(2024, 7, 15),
                        lookback_sessions=1)
    assert status is GateStatus.GREEN


def test_missing_day_red_at_production_lookback(clean_audit):
    """The RED path must hold in the production configuration (lookback=1, as
    replay.py runs it), not only at lookback=0 (review finding)."""
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 16, 22, 0, tzinfo=UTC)))
    status = ingest_day(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                        audit=audit, market="US", day=date(2024, 7, 16),  # no fixture data
                        lookback_sessions=1)
    assert status is GateStatus.RED


def test_non_trading_day_carries_forward_green(clean_audit):
    """Saturday after a clean Friday: gate is green, explicitly carried forward."""
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    s.execute(text("DELETE FROM market.data_quality_gates "
                   "WHERE market='US' AND gate_date IN ('2024-07-12','2024-07-13')"))
    adapter = FixtureAdapter(ROOT / "tests" / "fixtures")
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 13, 22, 0, tzinfo=UTC)))
    assert ingest_day(session=s, adapter=adapter, audit=audit, market="US",
                      day=date(2024, 7, 12), lookback_sessions=0) is GateStatus.GREEN
    status = ingest_day(session=s, adapter=adapter, audit=audit, market="US",
                        day=date(2024, 7, 13), lookback_sessions=1)
    assert status is GateStatus.GREEN
    gate = s.execute(text("SELECT reasons FROM market.data_quality_gates "
                          "WHERE market='US' AND gate_date='2024-07-13'")).scalar()
    assert "non-trading day" in str(gate)


def test_non_trading_day_carries_forward_red_not_false_green(clean_audit):
    """CRITICAL (review finding): a weekend gate must NOT go green after a red
    Friday — the latest-gate view would silently unblock downstream work while
    Friday's bars are still missing."""
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    s.execute(text("DELETE FROM market.data_quality_gates "
                   "WHERE market='US' AND gate_date IN ('2024-07-12','2024-07-13')"))
    write_gate(s, GateResult(market="US", gate_date=date(2024, 7, 12),
                             status=GateStatus.RED, reasons=("vendor outage",)))
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 13, 22, 0, tzinfo=UTC)))
    status = ingest_day(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                        audit=audit, market="US", day=date(2024, 7, 13),
                        lookback_sessions=1)
    assert status is GateStatus.RED
    gate = s.execute(text("SELECT status, reasons FROM market.data_quality_gates "
                          "WHERE market='US' AND gate_date='2024-07-13'")).one()
    assert gate.status == "red"
    assert "carried forward" in str(gate.reasons)
