"""FX daily job (task 1b): writes market.fx_rates_daily, audited, idempotent,
and honest about missing rates (review finding: the 'reports' half was untested)."""
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
    pairs = required_pairs(s)
    assert ("USD", "AUD") in pairs
    assert all(quote == "AUD" and base != "AUD" for base, quote in pairs)


def test_ingest_fx_writes_rate_and_audits(clean_audit):
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    day = date(2026, 7, 10)
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE rate_date = :d"), {"d": day})
    res = ingest_fx(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                    audit=_audit(s, day), day=day)
    assert res.written == 1
    assert res.missing == ()
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


def test_ingest_fx_missing_rate_writes_nothing_and_reports(clean_audit):
    s = clean_audit
    seed_instruments(s, ROOT / "seeds" / "instruments_seed.csv")
    day = date(2026, 7, 12)  # no fixture rate for this day
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE rate_date = :d"), {"d": day})
    res = ingest_fx(session=s, adapter=FixtureAdapter(ROOT / "tests" / "fixtures"),
                    audit=_audit(s, day), day=day)
    assert res.written == 0
    assert res.missing == ("USDAUD",)
    n = s.execute(text(
        "SELECT count(*) FROM market.fx_rates_daily WHERE rate_date=:d"), {"d": day}).scalar()
    assert n == 0
    # the 'reports' half: the audit payload must carry the missing pair
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type='market.fx.ingested' ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload["missing"] == ["USDAUD"]
    assert payload["written"] == 0
