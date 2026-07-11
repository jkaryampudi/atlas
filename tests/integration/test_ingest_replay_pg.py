from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.ingest import ingest_day, seed_instruments
from atlas.dcp.market_data.models import GateStatus
from atlas.dcp.risk.seed_limits import seed_limit_set
from tests.conftest import requires_pg

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]


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


def test_non_trading_day_gate_is_green_not_red(clean_audit):
    """Saturday: no bars are expected, so the gate must not be a false RED."""
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    audit = PostgresAuditLog(s, FrozenClock(datetime(2024, 7, 13, 22, 0, tzinfo=UTC)))
    status = ingest_day(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                        audit=audit, market="US", day=date(2024, 7, 13),
                        lookback_sessions=1)
    assert status is GateStatus.GREEN
    gate = s.execute(text("SELECT reasons FROM market.data_quality_gates "
                          "WHERE market='US' AND gate_date='2024-07-13'")).scalar()
    assert "non-trading day" in str(gate)
