"""FX daily job (task 1b): writes market.fx_rates_daily, audited, idempotent."""
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.fx import ingest_fx, required_pairs
from atlas.dcp.market_data.ingest import seed_instruments
from tests.conftest import requires_pg

pytestmark = requires_pg
ROOT = Path(__file__).parents[2]


def _audit(s, day: date) -> PostgresAuditLog:
    return PostgresAuditLog(s, FrozenClock(datetime(day.year, day.month, day.day, 22, tzinfo=UTC)))


def test_required_pairs_derived_from_instrument_currencies(clean_audit):
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    # Seed universe holds USD instruments and one AUD instrument; base is AUD.
    assert required_pairs(s) == [("USD", "AUD")]


def test_ingest_fx_writes_rate_and_audits(clean_audit):
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    day = date(2026, 7, 10)
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE rate_date = :d"), {"d": day})
    written = ingest_fx(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                        audit=_audit(s, day), day=day)
    assert written == 1
    row = s.execute(text(
        "SELECT rate, source FROM market.fx_rates_daily "
        "WHERE base='USD' AND quote='AUD' AND rate_date=:d"), {"d": day}).one()
    assert Decimal(row.rate) == Decimal("1.52")
    assert row.source == "FixtureAdapter"
    n = s.execute(text(
        "SELECT count(*) FROM audit.decision_events WHERE event_type='market.fx.ingested'"
    )).scalar()
    assert n == 1


def test_ingest_fx_is_idempotent(clean_audit):
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    day = date(2026, 7, 10)
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE rate_date = :d"), {"d": day})
    adapter = FixtureAdapter(ROOT / "tests" / "fixtures")
    ingest_fx(session=s, adapter=adapter, audit=_audit(s, day), day=day)
    ingest_fx(session=s, adapter=adapter, audit=_audit(s, day), day=day)
    n = s.execute(text(
        "SELECT count(*) FROM market.fx_rates_daily WHERE rate_date=:d"), {"d": day}).scalar()
    assert n == 1


def test_ingest_fx_missing_rate_writes_nothing_but_reports(clean_audit):
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    day = date(2026, 7, 12)  # no fixture rate for this day
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE rate_date = :d"), {"d": day})
    written = ingest_fx(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                        audit=_audit(s, day), day=day)
    assert written == 0
    n = s.execute(text(
        "SELECT count(*) FROM market.fx_rates_daily WHERE rate_date=:d"), {"d": day}).scalar()
    assert n == 0
