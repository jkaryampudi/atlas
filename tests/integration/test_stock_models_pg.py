"""The per-stock statistical model panel (stock_models.compute_models): a
steadily rising name reads bullish across the technical suite and momentum; a
falling name reads bearish; everything is point-in-time from adjusted bars.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import text

from atlas.dcp.research.stock_models import compute_models
from tests.conftest import requires_pg

pytestmark = requires_pg


def _instrument(s, sym):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, currency, is_active) VALUES (:s,'US','US','stock',:s,'USD',true) "
        "RETURNING id"), {"s": sym}).scalar()


def _seed(s, iid, start, closes):
    d = start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        s.execute(text(
            "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
            " high, low, close, volume, source) "
            "VALUES (:i,:d,:c,:hi,:lo,:c,1000,'EodhdAdapter')"),
            {"i": iid, "d": d, "c": c, "hi": c * 1.01, "lo": c * 0.99})
        d += timedelta(days=1)
    return d


def test_rising_stock_reads_bullish(pg_session):
    s = pg_session
    spy = _instrument(s, "SPY")
    stk = _instrument(s, "UPCO")
    start = date(2024, 1, 1)
    _seed(s, spy, start, [400.0 * (1.0005 ** i) for i in range(320)])
    _seed(s, stk, start, [50.0 * (1.004 ** i) for i in range(320)])   # strong uptrend

    m = compute_models(s, stk, "UPCO", date(2026, 1, 1))
    t = m["technical"]
    assert t["summary"] in ("Buy", "Strong Buy")
    assert t["bullish_signals"] >= t["total_signals"] - 1        # nearly all bullish
    assert m["price"] > t["sma_200"] > 0                         # price above the trend
    assert t["rsi_14"] > 50
    assert m["momentum"]["mom_12_1"] > 0
    assert m["momentum"]["rs_vs_spy_252d"] > 0                   # beats SPY
    assert m["risk"]["beta_vs_spy"] is not None
    assert m["technical"]["pct_of_52w_range"] > 80              # near the highs


def test_falling_stock_reads_bearish(pg_session):
    s = pg_session
    spy = _instrument(s, "SPY")
    stk = _instrument(s, "DNCO")
    start = date(2024, 1, 1)
    _seed(s, spy, start, [400.0 * (1.0005 ** i) for i in range(320)])
    _seed(s, stk, start, [200.0 * (0.996 ** i) for i in range(320)])   # steady downtrend

    m = compute_models(s, stk, "DNCO", date(2026, 1, 1))
    t = m["technical"]
    assert t["summary"] in ("Sell", "Strong Sell")
    assert m["price"] < t["sma_200"]                            # below the trend
    assert t["rsi_14"] < 50
    assert m["momentum"]["mom_12_1"] < 0
    assert t["pct_of_52w_range"] < 20                          # near the lows
