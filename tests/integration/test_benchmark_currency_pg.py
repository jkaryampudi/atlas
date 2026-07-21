"""F-006: the benchmark total return must be reported in the portfolio's AUD base
so it shares the sleeve's FX basis. A USD benchmark whose FX moves between the two
sessions must reflect that FX move (the AUD return), not the bare USD return."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.dcp.reporting.attribution import _tr_ret
from tests.conftest import requires_pg

pytestmark = requires_pg


def _usd_spy(s, prev_close, on_close):
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
        " name, currency, is_active) VALUES ('ZSPY','XNYS','US','etf','ZSPY','USD',true) "
        "RETURNING id")).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high,"
        " low, close, volume, source) VALUES "
        "(:i,'2026-07-13',:p,:p,:p,:p,1,'EodhdAdapter'),"
        "(:i,'2026-07-14',:o,:o,:o,:o,1,'EodhdAdapter')"),
        {"i": iid, "p": prev_close, "o": on_close})
    return iid


def test_benchmark_return_is_aud_including_fx_move(clean_audit):
    s = clean_audit
    _usd_spy(s, prev_close=100, on_close=110)      # USD price return +10%
    # FX strengthens USD/AUD from 1.5 to 1.6 (+6.67%) between the two sessions
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-13','1.5','x'),('USD','AUD','2026-07-14','1.6','x') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = excluded.rate"))
    r = _tr_ret(s, "ZSPY", date(2026, 7, 14), date(2026, 7, 13))
    # AUD return = (110*1.6)/(100*1.5) - 1 = 176/150 - 1 = 0.17333...
    assert r == pytest.approx(Decimal("176") / Decimal("150") - 1, abs=1e-9)
    # and it is NOT the bare USD return of 0.10
    assert abs(float(r) - 0.10) > 0.05


def test_flat_fx_reduces_to_the_usd_return(clean_audit):
    s = clean_audit
    _usd_spy(s, prev_close=100, on_close=110)
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-13','1.5','x'),('USD','AUD','2026-07-14','1.5','x') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = excluded.rate"))
    r = _tr_ret(s, "ZSPY", date(2026, 7, 14), date(2026, 7, 13))
    assert r == pytest.approx(Decimal("0.10"), abs=1e-9)   # flat FX -> USD return


def test_missing_fx_fails_closed(clean_audit):
    s = clean_audit
    _usd_spy(s, prev_close=100, on_close=110)
    # guarantee NO USD->AUD rate exists on or before the prev session (fx_to_aud
    # takes the latest rate <= date, so a leftover earlier rate would defeat the
    # "missing" case). rolled back with the session.
    s.execute(text("DELETE FROM market.fx_rates_daily WHERE base='USD' AND quote='AUD' "
                   "AND rate_date <= '2026-07-13'"))
    # only the 'on' date FX exists; the prev date is missing -> fail closed
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-14','1.6','x') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate='1.6'"))
    with pytest.raises(RuntimeError, match="rate"):
        _tr_ret(s, "ZSPY", date(2026, 7, 14), date(2026, 7, 13))
