"""F-015: the nightly incremental ingest must refresh dividends on the same path
as bars and splits — previously it never fetched them, so total-return / PEAD
inputs silently decayed."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text

from atlas.dcp.market_data.daily import _ingest_market
from atlas.dcp.market_data.models import Bar, Dividend
from tests.conftest import requires_pg

pytestmark = requires_pg


class _StubAdapter:
    """Serves one new session with a bar AND a dividend (and no splits) for the
    incremental window — the minimal surface _ingest_market touches."""
    def __init__(self, day: date) -> None:
        self._day = day

    def fetch_splits(self, symbol, start, end):
        return []

    def fetch_bars(self, symbol, start, end):
        return [Bar(symbol=symbol, bar_date=self._day, open=Decimal("100"),
                    high=Decimal("101"), low=Decimal("99"), close=Decimal("100"),
                    volume=1_000_000)]

    def fetch_dividends(self, symbol, start, end):
        return [Dividend(symbol=symbol, ex_date=self._day, amount=Decimal("0.55"),
                         currency="USD")]


def test_incremental_ingest_records_dividends(clean_audit):
    s = clean_audit
    # one active US instrument whose latest stored bar is the session BEFORE the
    # one the stub will serve (so the incremental window includes that day).
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency, is_active) VALUES ('ZDVR','XNYS','US','stock','ZDVR','USD',true) "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high,"
        " low, close, volume, source) VALUES (:i,'2024-07-12',100,101,99,100,1,"
        "'EodhdAdapter')"), {"i": iid})
    # next US session after Fri 2024-07-12 is Mon 2024-07-15.
    report = _ingest_market(s, _StubAdapter(date(2024, 7, 15)), "US",
                            datetime(2024, 7, 16, 2, tzinfo=UTC), [])
    assert report.dividends == 1
    stored = s.execute(text(
        "SELECT amount FROM market.corporate_actions WHERE instrument_id=:i "
        "AND action_type='dividend' AND action_date='2024-07-15'"), {"i": iid}).scalar()
    assert stored == Decimal("0.55")


def test_dividend_refresh_is_idempotent(clean_audit):
    s = clean_audit
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency, is_active) VALUES ('ZDVR2','XNYS','US','stock','ZDVR2','USD',true) "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high,"
        " low, close, volume, source) VALUES (:i,'2024-07-12',100,101,99,100,1,"
        "'EodhdAdapter')"), {"i": iid})
    r1 = _ingest_market(s, _StubAdapter(date(2024, 7, 15)), "US",
                        datetime(2024, 7, 16, 2, tzinfo=UTC), [])
    assert r1.dividends == 1
    # roll the latest bar back (delete the new 07-15 bar) and re-ingest the same
    # window: the dividend already exists -> not double-counted (idempotent).
    s.execute(text("DELETE FROM market.price_bars_daily "
                   "WHERE instrument_id=:i AND bar_date='2024-07-15'"), {"i": iid})
    r2 = _ingest_market(s, _StubAdapter(date(2024, 7, 15)), "US",
                        datetime(2024, 7, 16, 2, tzinfo=UTC), [])
    assert r2.dividends == 0
    assert s.execute(text(
        "SELECT count(*) FROM market.corporate_actions WHERE instrument_id=:i "
        "AND action_type='dividend'"), {"i": iid}).scalar() == 1
