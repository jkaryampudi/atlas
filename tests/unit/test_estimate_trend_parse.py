"""Earnings::Trend parse: the probed vendor shape (decimal strings, genuine
nulls, stale periods back to 2017), the structural near-period window boundary,
and the fixture-adapter route. Pure — no database."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.estimate_snapshots import (
    FUTURE_WINDOW_DAYS,
    PAST_WINDOW_DAYS,
    parse_estimate_trend,
)

TODAY = date(2026, 7, 17)

# The re-verified live shape (AAPL 2026-03-31, probed 2026-07-17): decimal
# STRINGS, one null revisions leg, vendor extras we deliberately do not store.
AAPL_PERIOD = {
    "date": "2026-03-31", "period": "0q", "growth": "0.1771",
    "earningsEstimateAvg": "1.9404", "earningsEstimateLow": "1.5600",
    "earningsEstimateHigh": "2.1600", "earningsEstimateYearAgoEps": "1.6500",
    "earningsEstimateNumberOfAnalysts": "33.0000",
    "earningsEstimateGrowth": "0.1760",
    "revenueEstimateAvg": "109578690550.00",
    "revenueEstimateLow": "104636000000.00",
    "revenueEstimateHigh": "115368900000.00",
    "revenueEstimateYearAgoEps": None, "revenueEstimateNumberOfAnalysts": "32.00",
    "revenueEstimateGrowth": "0.1491",
    "epsTrendCurrent": "1.9404", "epsTrend7daysAgo": "1.9428",
    "epsTrend30daysAgo": "1.9514", "epsTrend60daysAgo": "1.9489",
    "epsTrend90daysAgo": "1.9428",
    "epsRevisionsUpLast7days": "3.0000", "epsRevisionsUpLast30days": "4.0000",
    "epsRevisionsDownLast7days": None, "epsRevisionsDownLast30days": "1.0000",
}


def _doc(trend: dict) -> dict:
    return {"General": {"Code": "ZEST"}, "Earnings": {"Trend": trend}}


def test_parse_probed_shape_field_mapping():
    rows = parse_estimate_trend(_doc({"2026-03-31": AAPL_PERIOD}), today=TODAY)
    assert len(rows) == 1
    r = rows[0]
    assert r.fiscal_period_end == date(2026, 3, 31)
    assert r.eps_estimate_avg == Decimal("1.9404")
    assert r.eps_estimate_analysts == Decimal("33.0000")
    assert r.revenue_estimate_avg == Decimal("109578690550.00")
    assert r.eps_trend_current == Decimal("1.9404")
    assert r.eps_trend_7d == Decimal("1.9428")
    assert r.eps_trend_30d == Decimal("1.9514")
    assert r.revisions_up_7d == Decimal("3.0000")
    assert r.revisions_up_30d == Decimal("4.0000")
    assert r.revisions_down_7d is None       # the vendor's genuine null
    assert r.revisions_down_30d == Decimal("1.0000")


def test_parse_sorts_by_fiscal_period_end():
    trend = {"2026-09-30": dict(AAPL_PERIOD), "2026-06-30": dict(AAPL_PERIOD)}
    rows = parse_estimate_trend(_doc(trend), today=TODAY)
    assert [r.fiscal_period_end.isoformat() for r in rows] == \
        ["2026-06-30", "2026-09-30"]


def test_near_period_window_boundary_is_inclusive():
    """Exactly today-PAST and today+FUTURE are kept; one day beyond each is
    dropped — the structural horizon, pinned so a drift is a loud test fail."""
    inside_lo = (TODAY - timedelta(days=PAST_WINDOW_DAYS)).isoformat()
    outside_lo = (TODAY - timedelta(days=PAST_WINDOW_DAYS + 1)).isoformat()
    inside_hi = (TODAY + timedelta(days=FUTURE_WINDOW_DAYS)).isoformat()
    outside_hi = (TODAY + timedelta(days=FUTURE_WINDOW_DAYS + 1)).isoformat()
    trend = {k: dict(AAPL_PERIOD) for k in
             (inside_lo, outside_lo, inside_hi, outside_hi)}
    rows = parse_estimate_trend(_doc(trend), today=TODAY)
    assert [r.fiscal_period_end.isoformat() for r in rows] == \
        [inside_lo, inside_hi]


def test_stale_vendor_periods_are_dropped():
    # the live probe returned periods back to 2017 in the same block
    trend = {"2017-06-30": dict(AAPL_PERIOD), "2026-06-30": dict(AAPL_PERIOD)}
    rows = parse_estimate_trend(_doc(trend), today=TODAY)
    assert [r.fiscal_period_end.isoformat() for r in rows] == ["2026-06-30"]


def test_malformed_and_valueless_periods_are_dropped():
    trend = {
        "not-a-date": dict(AAPL_PERIOD),          # unparseable key
        "2026-06-30": "free text, not a dict",    # non-dict row
        # every archived leg null/absent/hostile: nothing to preserve
        "2026-09-30": {"earningsEstimateAvg": None, "epsTrendCurrent": "N/A",
                       "epsRevisionsUpLast7days": True},
    }
    assert parse_estimate_trend(_doc(trend), today=TODAY) == []


def test_missing_trend_block_is_a_valid_empty_answer():
    assert parse_estimate_trend({}, today=TODAY) == []
    assert parse_estimate_trend({"Earnings": {}}, today=TODAY) == []
    assert parse_estimate_trend({"Earnings": {"Trend": "nope"}}, today=TODAY) == []
    assert parse_estimate_trend({"Earnings": {"History": {}}}, today=TODAY) == []


def test_fixture_adapter_route_parses_the_trend_shape(tmp_path):
    """The fixture-adapter contract carries Earnings.Trend exactly like the
    vendor document: fundamentals/{symbol}.json, whole."""
    root = tmp_path / "fixtures"
    (root / "fundamentals").mkdir(parents=True)
    (root / "fundamentals" / "ZEST.json").write_text(json.dumps(
        _doc({"2026-03-31": AAPL_PERIOD, "2017-09-30": dict(AAPL_PERIOD)})))
    payload = FixtureAdapter(root).fetch_fundamentals("ZEST")
    rows = parse_estimate_trend(payload, today=TODAY)
    assert [r.fiscal_period_end.isoformat() for r in rows] == ["2026-03-31"]
    assert rows[0].eps_estimate_avg == Decimal("1.9404")
    assert rows[0].revisions_down_7d is None
