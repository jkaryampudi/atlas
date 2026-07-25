"""F-006: the authoritative reporting-basis benchmark service (market_data/
benchmark.py) — AUD conversion, total-return reinvestment, and fail-closed on
missing FX, proven directly against the database."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.dcp.market_data.benchmark import (benchmark_reporting_series,
                                             reporting_close_series)
from tests.conftest import requires_pg

pytestmark = requires_pg

D0 = date(2026, 3, 2)
D1 = date(2026, 3, 3)


def _inst(s, symbol, currency="USD", exchange="XTEST"):
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, sector_gics, currency, is_active) "
        "VALUES (:s,:ex,'US','stock',:s,'Information Technology',:c,true) RETURNING id"),
        {"s": symbol, "ex": exchange, "c": currency}).scalar())


def _bar(s, iid, on, close):
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        " low, close, volume, source) VALUES (:i,:d,:c,:c,:c,:c,1000,'EodhdAdapter') "
        "ON CONFLICT (instrument_id, bar_date) DO UPDATE SET close = EXCLUDED.close"),
        {"i": iid, "d": on, "c": close})


def _fx(s, base, on, rate):
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES (:b,'AUD',:d,:r,'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"b": base, "d": on, "r": rate})


def _div(s, iid, ex_date, amount, currency="USD"):
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_type, "
        " action_date, amount, currency, source) "
        "VALUES (:i,'dividend',:d,:a,:c,'test')"),
        {"i": iid, "d": ex_date, "a": amount, "c": currency})


def test_reporting_close_series_converts_price_to_aud(pg_session):
    s = pg_session
    iid = _inst(s, "ZAUD")
    _bar(s, iid, D0, "100")
    _fx(s, "USD", D0, "1.5")
    series = reporting_close_series(s, instrument_id=iid, symbol="ZAUD",
                                    currency="USD", through=D0)
    assert series[D0] == Decimal("150.0")          # 100 USD x 1.5 USD/AUD


def test_reporting_close_series_reinvests_dividends_total_return(pg_session):
    s = pg_session
    iid = _inst(s, "ZDIV")
    _bar(s, iid, D0, "100")
    _bar(s, iid, D1, "100")                          # flat price
    _div(s, iid, D1, "10")                           # 10% dividend, ex-date D1
    _fx(s, "USD", D0, "1.0")
    series = reporting_close_series(s, instrument_id=iid, symbol="ZDIV",
                                    currency="USD", through=D1)
    # price is flat, but the dividend reinvested at the D1 close lifts the D1
    # total-return close above D0 (the reinvestment factor steps at the ex-date).
    # TR runs in float panel space, so compare within a tiny tolerance.
    assert abs(series[D0] - Decimal("100")) < Decimal("0.001")
    assert abs(series[D1] - Decimal("110")) < Decimal("0.001")   # 100 x (1 + 10/100)
    assert series[D1] > series[D0]


def test_reporting_close_series_fails_closed_without_fx(pg_session):
    s = pg_session
    # XTS is the ISO-4217 'reserved for testing' code — no suite ever seeds an
    # XTS->AUD rate, so fx_to_aud is guaranteed to fail closed here regardless of
    # what committed FX rows an earlier test file left behind.
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE base = 'XTS'"))
    iid = _inst(s, "ZNOFX", currency="XTS")
    _bar(s, iid, D0, "100")
    with pytest.raises(RuntimeError, match="rate"):
        reporting_close_series(s, instrument_id=iid, symbol="ZNOFX",
                               currency="XTS", through=D0)


def test_benchmark_series_empty_when_spy_absent(pg_session):
    s = pg_session
    s.execute(text("UPDATE market.instruments SET is_active = false WHERE symbol = 'SPY'"))
    assert benchmark_reporting_series(s, through=D0) == {}
