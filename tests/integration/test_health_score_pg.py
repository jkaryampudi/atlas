"""Atlas health score (health_score.compute_health_score): factor percentiles vs
the universe roll into five pillars and a composite. The synthetic universe is
arranged so the SUBJECT sits at the 0.75 percentile on every factor -> every
pillar 75.0 -> composite 75.0 (rating 4). Also: momentum split-exclusion and
fail-soft when the subject has no fundamentals.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import text

from atlas.dcp.research.health_score import compute_health_score
from tests.conftest import requires_pg

pytestmark = requires_pg

_AS_OF = date(2026, 1, 1)


def _instrument(s, sym, active=True):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, currency, is_active, sector_gics) "
        "VALUES (:s,'US','US','stock',:s,'USD',:a,'TestTech') RETURNING id"),
        {"s": sym, "a": active}).scalar()


def _payload(pe, evebitda, pb, ps, roe, gp, om, pm, rg, eg, mcap, fcf, rev=100.0):
    return {
        "Valuation": {"TrailingPE": pe, "EnterpriseValueEbitda": evebitda,
                      "PriceBookMRQ": pb, "PriceSalesTTM": ps},
        "Highlights": {"MarketCapitalization": mcap, "ReturnOnEquityTTM": roe,
                       "GrossProfitTTM": gp, "RevenueTTM": rev,
                       "OperatingMarginTTM": om, "ProfitMargin": pm,
                       "QuarterlyRevenueGrowthYOY": rg,
                       "QuarterlyEarningsGrowthYOY": eg},
        "Financials": {"Cash_Flow": {"yearly": {
            "2025-12-31": {"date": "2025-12-31", "freeCashFlow": fcf}}}},
    }


def _fund(s, iid, payload):
    s.execute(text(
        "INSERT INTO market.fundamentals (instrument_id, as_of, payload, source) "
        "VALUES (:i, :d, CAST(:p AS jsonb), 'EodhdAdapter')"),
        {"i": iid, "d": date(2025, 12, 31), "p": json.dumps(payload)})


def _seed_momentum(s, iid, prior, last):
    """252 daily bars ending at _AS_OF, all at `prior` except the newest = `last`
    (so trailing 1-year return = last/prior - 1)."""
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        " low, close, volume, source) "
        "SELECT :i, gs::date, :p, :p, :p, :p, 1000, 'EodhdAdapter' "
        "FROM generate_series(:start, :on, '1 day') gs"),
        {"i": iid, "p": prior, "start": _AS_OF - timedelta(days=251), "on": _AS_OF})
    s.execute(text(
        "UPDATE market.price_bars_daily SET close = :l "
        "WHERE instrument_id = :i AND bar_date = :on"),
        {"l": last, "i": iid, "on": _AS_OF})


# (PE, EV/EBITDA, P/B, P/S, ROE, GP, OpMargin, ProfitMargin, RevGrowth, EpsGrowth,
#  MarketCap, FCF), then momentum (prior->last). Subject is deliberately the
# 3rd-best of 4 on every factor -> percentile 0.75 everywhere.
_NAMES = {
    "SUBJ": (dict(pe=20, evebitda=10, pb=4, ps=5, roe=0.15, gp=50, om=0.2, pm=0.1,
                  rg=0.1, eg=0.2, mcap=1000, fcf=50), (100.0, 110.0)),   # ret +0.10
    "PONE": (dict(pe=10, evebitda=5, pb=2, ps=2.5, roe=0.30, gp=70, om=0.4, pm=0.2,
                  rg=0.3, eg=0.5, mcap=1000, fcf=100), (100.0, 150.0)),  # ret +0.50
    "PTWO": (dict(pe=40, evebitda=20, pb=8, ps=10, roe=0.10, gp=40, om=0.15, pm=0.08,
                  rg=0.05, eg=0.1, mcap=1000, fcf=30), (100.0, 105.0)),  # ret +0.05
    "PTHR": (dict(pe=80, evebitda=40, pb=16, ps=20, roe=0.05, gp=30, om=0.1, pm=0.05,
                  rg=0.02, eg=0.05, mcap=1000, fcf=20), (100.0, 90.0)),  # ret -0.10
}


def _seed_universe(s):
    ids = {}
    for name, (f, (prior, last)) in _NAMES.items():
        iid = _instrument(s, name)
        _fund(s, iid, _payload(**f))
        _seed_momentum(s, iid, prior, last)
        ids[name] = iid
    return ids


def test_health_score_golden(pg_session):
    s = pg_session
    ids = _seed_universe(s)
    h = compute_health_score(s, ids["SUBJ"], "SUBJ", _AS_OF)

    assert h["universe_n"] == 4
    # subject is 3rd-of-4 on every factor -> percentile 0.75 everywhere
    rv = h["pillars"]["relative_value"]
    assert rv["factors"]["earnings_yield"]["percentile"] == 0.75
    assert rv["score"] == 75.0 and rv["rating"] == 4
    for pkey in ("relative_value", "profitability", "growth", "cash_flow", "momentum"):
        assert h["pillars"][pkey]["score"] == 75.0, pkey
    # momentum factor value = +0.10 (last/prior - 1) at the 0.75 percentile
    assert abs(h["pillars"]["momentum"]["factors"]["return_1y"]["value"] - 0.10) < 1e-9
    assert h["pillars"]["momentum"]["factors"]["return_1y"]["percentile"] == 0.75
    # composite = mean of five 0.75 pillars
    assert h["composite"]["score"] == 75.0
    assert h["composite"]["rating"] == 4
    assert h["composite"]["n_pillars"] == 5


def test_momentum_excludes_split_names(pg_session):
    s = pg_session
    ids = _seed_universe(s)
    # a split on the SUBJECT inside the window -> its raw return is unreliable, so
    # the momentum pillar drops for it (fail-soft, not a false number)
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_type, "
        " action_date, ratio, source) VALUES (:i,'split',:d,'2',:src)"),
        {"i": ids["SUBJ"], "d": date(2025, 6, 1), "src": "EodhdAdapter"})
    h = compute_health_score(s, ids["SUBJ"], "SUBJ", _AS_OF)
    assert h["pillars"]["momentum"]["score"] is None            # excluded
    assert h["pillars"]["momentum"]["factors"]["return_1y"]["percentile"] is None
    # the other four pillars still compute; composite averages only those
    assert h["composite"]["n_pillars"] == 4
    assert h["composite"]["score"] == 75.0                      # the 4 remaining are 75


def test_malformed_cashflow_does_not_crash_universe(pg_session):
    s = pg_session
    ids = _seed_universe(s)
    # a universe name whose Cash_Flow.yearly is a NON-OBJECT (EODHD sends null /
    # arrays for some names) — unguarded, jsonb_each would abort the whole scan
    bad = _instrument(s, "BADCF")
    p = _payload(pe=15, evebitda=8, pb=3, ps=4, roe=0.2, gp=55, om=0.25, pm=0.12,
                 rg=0.15, eg=0.25, mcap=1000, fcf=40)
    p["Financials"]["Cash_Flow"]["yearly"] = None
    _fund(s, bad, p)
    _seed_momentum(s, bad, 100.0, 108.0)
    # must NOT raise, and must still score the subject across the full universe
    h = compute_health_score(s, ids["SUBJ"], "SUBJ", _AS_OF)
    assert h["universe_n"] == 5
    assert h["composite"]["score"] is not None


def test_fail_soft_no_fundamentals(pg_session):
    s = pg_session
    _seed_universe(s)
    # an active US name with NO fundamentals is not in the universe -> empty score
    orphan = _instrument(s, "ORPH")
    h = compute_health_score(s, orphan, "ORPH", _AS_OF)
    assert h["composite"]["score"] is None
    assert h["composite"]["n_pillars"] == 0
    assert h["pillars"] == {}
