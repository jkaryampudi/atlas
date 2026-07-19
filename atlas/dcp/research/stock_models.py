"""Per-stock STATISTICAL MODEL PANEL for the research dossier — the full quant
readout a pro-research report shows, computed DETERMINISTICALLY and POINT-IN-TIME
from the stock's own split-adjusted bars and its latest fundamentals snapshot
(nothing knowable only after `as_of`).

Every number is a mechanical computation or a vendor-reported fact — no
forecasts are fabricated (Constitution: no invented numbers). The technical
"summary" is a rules-based aggregation of standard indicators (like the
technical-summary on any charting site), labelled as such, not a prediction.

Sections: technical (MAs, RSI, MACD, stochastic, ATR, 52-week range +
aggregate signal), momentum / relative strength, valuation, quality, risk
(volatility, beta vs SPY).
"""
from __future__ import annotations

import math
import statistics
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.indicators.core import rsi, sma, wilder_atr
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.fundamentals import _get, _number
from atlas.dcp.market_data.models import Bar, Split

VENDOR_SOURCE = "EodhdAdapter"


def _adjusted_bars(session: Session, instrument_id: str,
                   through: date) -> list[tuple[date, float, float, float, float]]:
    """Ascending split-adjusted OHLC bars <= `through` (adjust-on-read, only
    splits recorded by `through` — no look-ahead)."""
    rows = session.execute(text(
        "SELECT bar_date, open, high, low, close FROM market.price_bars_daily "
        "WHERE instrument_id = :i AND source = :src AND close IS NOT NULL "
        "  AND bar_date <= :d ORDER BY bar_date"),
        {"i": instrument_id, "src": VENDOR_SOURCE, "d": through}).all()
    splits = [Split(symbol=str(instrument_id), action_date=r.action_date,
                    ratio=Decimal(r.ratio))
              for r in session.execute(text(
                  "SELECT action_date, ratio FROM market.corporate_actions "
                  "WHERE instrument_id = :i AND action_type = 'split' "
                  "  AND action_date <= :d ORDER BY action_date"),
                  {"i": instrument_id, "d": through}).all()]
    bars = [Bar(symbol=str(instrument_id), bar_date=r.bar_date,
                open=r.open if r.open is not None else r.close,
                high=r.high if r.high is not None else r.close,
                low=r.low if r.low is not None else r.close,
                close=r.close, volume=0) for r in rows]
    if splits:
        bars = adjust_for_splits(bars, splits)
    return [(b.bar_date, float(b.open), float(b.high), float(b.low), float(b.close))
            for b in bars]


def _ema_series(values: list[float], span: int) -> list[float]:
    """EMA seeded with the SMA of the first `span` points; returns one value per
    input from index span-1 onward (empty if too short)."""
    if len(values) < span:
        return []
    k = 2.0 / (span + 1)
    out = [sum(values[:span]) / span]
    for v in values[span:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _ema(values: list[float], span: int) -> float | None:
    s = _ema_series(values, span)
    return s[-1] if s else None


def _mom_12_1(closes: list[float]) -> float | None:
    """12-1 momentum: close[t-21] / close[t-252] - 1 (production convention)."""
    if len(closes) <= 252 or closes[-252] <= 0:
        return None
    return closes[-21] / closes[-252] - 1.0


def _ret(closes: list[float], lag: int) -> float | None:
    if len(closes) <= lag or closes[-1 - lag] <= 0:
        return None
    return closes[-1] / closes[-1 - lag] - 1.0


def _last(seq: list[float | None]) -> float | None:
    return seq[-1] if seq and seq[-1] is not None else None


def _fund(payload: dict[str, object], path: tuple[str, str]) -> float | None:
    rendered = _number(_get(payload, path))
    if rendered is None:
        return None
    try:
        return float(rendered)
    except ValueError:
        return None


def compute_models(session: Session, instrument_id: str, symbol: str,
                   as_of: date) -> dict[str, object]:
    """The full model panel for one stock at `as_of`. Fields are None where
    history/fundamentals are insufficient (fail-soft, never fabricated)."""
    bars = _adjusted_bars(session, instrument_id, as_of)
    closes = [c for _, _, _, _, c in bars]
    highs = [h for _, _, h, _, _ in bars]
    lows = [lo for _, _, _, lo, _ in bars]
    px = closes[-1] if closes else None

    # ---- technical ----
    ma = {w: (_last(sma(closes, w)) if len(closes) >= w else None)
          for w in (20, 50, 100, 200)}
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    rsi14 = _last(rsi(closes, 14)) if len(closes) >= 15 else None
    # MACD(12,26,9): line = EMA12 - EMA26 (aligned tails); signal = EMA9 of the
    # line series; histogram = line - signal (the standard bullish read).
    e12, e26 = _ema_series(closes, 12), _ema_series(closes, 26)
    macd = macd_sig = macd_hist = None
    if e12 and e26:
        m = min(len(e12), len(e26))
        line = [e12[-m + i] - e26[-m + i] for i in range(m)]
        macd = line[-1]
        sig_series = _ema_series(line, 9)
        if sig_series:
            macd_sig = sig_series[-1]
            macd_hist = macd - macd_sig
    atr = _last(wilder_atr(highs, lows, closes, 14)) if len(closes) >= 15 else None
    atr_pct = (atr / px) if (atr is not None and px) else None
    # stochastic %K(14)
    stoch = None
    if len(closes) >= 14 and px is not None:
        hi14, lo14 = max(highs[-14:]), min(lows[-14:])
        if hi14 > lo14:
            stoch = 100.0 * (px - lo14) / (hi14 - lo14)
    hi52 = max(highs[-252:]) if highs else None
    lo52 = min(lows[-252:]) if lows else None
    range_pos = (100.0 * (px - lo52) / (hi52 - lo52)
                 if (hi52 is not None and lo52 is not None and hi52 > lo52 and px) else None)

    # aggregate technical signal — count bullish across independent reads.
    votes: list[bool] = []
    for w in (20, 50, 100, 200):
        mw = ma[w]
        if mw is not None and px is not None:
            votes.append(px > mw)
    if ema20 is not None and px is not None:
        votes.append(px > ema20)
    if ema50 is not None and px is not None:
        votes.append(px > ema50)
    if rsi14 is not None:
        votes.append(rsi14 > 50)
    if macd_hist is not None:
        votes.append(macd_hist > 0)
    if stoch is not None:
        votes.append(stoch > 50)
    bull = sum(1 for v in votes if v)
    total = len(votes)
    frac = (bull / total) if total else None
    # trend language, NOT recommendation language: this is a rules-based read of
    # the price TREND (like a charting site's technical summary), never an Atlas
    # buy/sell call, so "Bullish/Bearish" — not "Buy/Sell" — is deliberate.
    summary = (None if frac is None else
               "Strongly Bullish" if frac >= 0.8 else "Bullish" if frac >= 0.6 else
               "Neutral" if frac >= 0.4 else "Bearish" if frac >= 0.2 else "Strongly Bearish")

    # ---- momentum / relative strength (vs SPY) ----
    spy_iid = session.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'SPY' "
        "ORDER BY is_active DESC LIMIT 1")).scalar()
    spy_closes = [c for _, _, _, _, c in _adjusted_bars(session, spy_iid, as_of)] \
        if spy_iid is not None else []
    rs_252: float | None = None
    stock_1y, spy_1y = _ret(closes, 252), _ret(spy_closes, 252)
    if stock_1y is not None and spy_1y is not None:
        rs_252 = stock_1y - spy_1y

    # ---- risk: beta + vol ----
    beta = None
    n = min(len(closes), len(spy_closes))
    if n >= 64:
        sr = [closes[-i] / closes[-i - 1] - 1.0 for i in range(1, 63)
              if closes[-i - 1] > 0]
        mr = [spy_closes[-i] / spy_closes[-i - 1] - 1.0 for i in range(1, 63)
              if spy_closes[-i - 1] > 0]
        m = min(len(sr), len(mr))
        if m >= 30 and statistics.pvariance(mr[:m]) > 0:
            beta = statistics.covariance(sr[:m], mr[:m]) / statistics.pvariance(mr[:m])
    daily = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))
             if closes[i - 1] > 0]
    vol20 = (statistics.pstdev(daily[-20:]) * math.sqrt(252)
             if len(daily) >= 20 else None)

    # ---- valuation + quality (latest fundamentals <= as_of) ----
    payload = session.execute(text(
        "SELECT payload FROM market.fundamentals WHERE instrument_id = :i "
        "  AND as_of <= :on ORDER BY as_of DESC LIMIT 1"),
        {"i": instrument_id, "on": as_of}).scalar()
    p = payload if isinstance(payload, dict) else None
    # always the same shape (None where fundamentals are absent) — the dossier
    # contract never depends on whether a snapshot was found.
    valuation: dict[str, float | None] = dict.fromkeys(
        ("trailing_pe", "forward_pe", "ps_ttm", "ev_ebitda", "price_book",
         "peg", "dividend_yield", "market_cap"))
    quality: dict[str, float | None] = dict.fromkeys(
        ("roe", "gross_margin", "operating_margin", "profit_margin",
         "revenue_growth_yoy", "eps_ttm"))
    analyst_target = upside = None
    if p is not None:
        valuation.update({
            "trailing_pe": _fund(p, ("Valuation", "TrailingPE")),
            "forward_pe": _fund(p, ("Valuation", "ForwardPE")),
            "ps_ttm": _fund(p, ("Valuation", "PriceSalesTTM")),
            "ev_ebitda": _fund(p, ("Valuation", "EnterpriseValueEbitda")),
            "price_book": _fund(p, ("Valuation", "PriceBookMRQ")),
            "peg": _fund(p, ("Highlights", "PEGRatio")),
            "dividend_yield": _fund(p, ("Highlights", "DividendYield")),
            "market_cap": _fund(p, ("Highlights", "MarketCapitalization")),
        })
        rev = _fund(p, ("Highlights", "RevenueTTM"))
        gp = _fund(p, ("Highlights", "GrossProfitTTM"))
        quality.update({
            "roe": _fund(p, ("Highlights", "ReturnOnEquityTTM")),
            "gross_margin": (gp / rev) if (gp and rev) else None,
            "operating_margin": _fund(p, ("Highlights", "OperatingMarginTTM")),
            "profit_margin": _fund(p, ("Highlights", "ProfitMargin")),
            "revenue_growth_yoy": _fund(p, ("Highlights", "QuarterlyRevenueGrowthYOY")),
            "eps_ttm": _fund(p, ("Highlights", "EarningsShare")),
        })
        analyst_target = _fund(p, ("AnalystRatings", "TargetPrice"))
        upside = (analyst_target / px - 1.0) if (analyst_target and px) else None

    return {
        "price": px, "as_of": as_of.isoformat(),
        "technical": {
            "sma_20": ma[20], "sma_50": ma[50], "sma_100": ma[100], "sma_200": ma[200],
            "ema_20": ema20, "ema_50": ema50, "rsi_14": rsi14,
            "macd": macd, "macd_signal": macd_sig, "macd_hist": macd_hist,
            "stochastic_k": stoch, "atr_pct": atr_pct,
            "high_52w": hi52, "low_52w": lo52, "pct_of_52w_range": range_pos,
            "bullish_signals": bull, "total_signals": total, "summary": summary,
        },
        "momentum": {
            "ret_20d": _ret(closes, 20), "ret_63d": _ret(closes, 63),
            "ret_126d": _ret(closes, 126), "ret_252d": _ret(closes, 252),
            "mom_12_1": _mom_12_1(closes),
            "rs_vs_spy_252d": rs_252,
        },
        "valuation": {**valuation, "analyst_target": analyst_target, "upside_pct": upside},
        "quality": quality,
        "risk": {"vol_20d_ann": vol20, "beta_vs_spy": beta, "atr_pct": atr_pct},
    }
