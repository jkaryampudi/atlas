"""The financials panel (financials_panel.compute_financials): reported
statements, earnings surprises, and forward consensus are surfaced verbatim
from stored vendor data, POINT-IN-TIME to `as_of` — a period that ends after
as_of, a report announced after as_of, and a consensus snapshot taken after
as_of are all excluded; derived FCF yield is FCF / market cap.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import text

from atlas.dcp.research.financials_panel import compute_financials
from tests.conftest import requires_pg

pytestmark = requires_pg

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _instrument(s, sym):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, currency, is_active) VALUES (:s,'US','US','stock',:s,'USD',true) "
        "RETURNING id"), {"s": sym}).scalar()


_PAYLOAD = {
    "General": {"CurrencyCode": "USD"},
    "Highlights": {
        "MarketCapitalization": "800000000000.00",
        "RevenueTTM": "37454000000.00",
        "BookValue": "39.55",
        "EPSEstimateCurrentYear": "7.41",
        "EPSEstimateNextYear": "13.28",
        "WallStreetTargetPrice": "516.13",
    },
    "Technicals": {"Beta": "2.47"},
    "SharesStats": {"SharesOutstanding": "1630600639"},
    "Financials": {
        "Income_Statement": {"currency_symbol": "USD", "yearly": {
            "2025-12-31": {"date": "2025-12-31", "totalRevenue": "34639000000.00",
                           "grossProfit": "18183000000.00", "netIncome": "4335000000.00"},
            # ends AFTER as_of -> must be excluded (point-in-time)
            "2026-12-31": {"date": "2026-12-31", "totalRevenue": "49400000000.00",
                           "netIncome": "9000000000.00"},
        }, "quarterly": {
            "2025-09-30": {"date": "2025-09-30", "totalRevenue": "9246000000.00"},
        }},
        "Balance_Sheet": {"yearly": {
            "2025-12-31": {"date": "2025-12-31", "totalAssets": "76926000000.00",
                           "totalLiab": "13927000000.00",
                           "totalStockholderEquity": "62999000000.00",
                           "shortLongTermDebtTotal": "4006000000.00"},
        }},
        "Cash_Flow": {"yearly": {
            "2025-12-31": {"date": "2025-12-31",
                           "totalCashFromOperatingActivities": "7709000000.00",
                           "freeCashFlow": "2600000000.00"},
        }},
    },
}


def _fundamentals(s, iid, as_of, payload):
    s.execute(text(
        "INSERT INTO market.fundamentals (instrument_id, as_of, payload, source) "
        "VALUES (:i,:d,CAST(:p AS jsonb),'EodhdAdapter')"),
        {"i": iid, "d": as_of, "p": json.dumps(payload)})


def _surprise(s, iid, fpe, rd, actual, est, spct):
    s.execute(text(
        "INSERT INTO market.earnings_surprises (instrument_id, fiscal_period_end, "
        " report_date, eps_actual, eps_estimate, surprise_pct, source, fetched_at) "
        "VALUES (:i,:f,:r,:a,:e,:sp,'EodhdAdapter',:t)"),
        {"i": iid, "f": fpe, "r": rd, "a": actual, "e": est, "sp": spct, "t": _NOW})


def _estimate(s, iid, fpe, sd, eps_avg, analysts, rev):
    s.execute(text(
        "INSERT INTO market.estimate_snapshots (instrument_id, fiscal_period_end, "
        " snapshot_date, eps_estimate_avg, eps_estimate_analysts, revenue_estimate_avg, "
        " source, fetched_at) VALUES (:i,:f,:s,:e,:a,:r,'EodhdAdapter',:t)"),
        {"i": iid, "f": fpe, "s": sd, "e": eps_avg, "a": analysts, "r": rev, "t": _NOW})


def test_panel_is_point_in_time_and_verbatim(pg_session):
    s = pg_session
    iid = _instrument(s, "TSTA")
    as_of = date(2026, 1, 1)
    _fundamentals(s, iid, date(2025, 12, 31), _PAYLOAD)

    # earnings: one announced before as_of (kept), one announced after (dropped)
    _surprise(s, iid, date(2025, 9, 30), date(2025, 11, 1), "0.75", "0.70", "7.1")
    _surprise(s, iid, date(2025, 12, 31), date(2026, 2, 1), "0.84", "0.80", "5.0")

    # forward consensus: same forward period snapshot twice (latest <= as_of wins);
    # a backward period (dropped by fiscal_period_end >= as_of); a future snapshot (dropped)
    _estimate(s, iid, date(2026, 12, 31), date(2025, 12, 15), "13.00", "44", "48000000000")
    _estimate(s, iid, date(2026, 12, 31), date(2025, 12, 30), "13.28", "46", "49400000000")
    _estimate(s, iid, date(2025, 6, 30), date(2025, 12, 30), "6.00", "40", "30000000000")
    _estimate(s, iid, date(2026, 12, 31), date(2026, 6, 1), "14.00", "47", "50000000000")

    f = compute_financials(s, iid, "TSTA", as_of)

    assert f["currency"] == "USD"
    assert f["snapshot_as_of"] == "2025-12-31"

    # ---- statements: PIT + verbatim ----
    inc = f["statements"]["income"]
    periods = [r["period"] for r in inc["annual"]]
    assert periods == ["2025-12-31"]                       # 2026 period excluded (> as_of)
    row = inc["annual"][0]["values"]
    assert row["Revenue"] == 34639000000.0                 # rendered verbatim
    assert row["Net Income"] == 4335000000.0
    bal = f["statements"]["balance"]["annual"][0]["values"]
    assert bal["Total Equity"] == 62999000000.0            # totalStockholderEquity key
    assert bal["Total Debt"] == 4006000000.0               # shortLongTermDebtTotal key
    assert f["statements"]["cash_flow"]["annual"][0]["values"]["Free Cash Flow"] == 2600000000.0
    assert [r["period"] for r in inc["quarterly"]] == ["2025-09-30"]

    # ---- earnings history: only reports announced by as_of ----
    hist = f["earnings"]["history"]
    assert [h["fiscal_period_end"] for h in hist] == ["2025-09-30"]
    assert hist[0]["eps_actual"] == 0.75 and hist[0]["surprise_pct"] == 7.1

    # ---- forward consensus: latest snapshot <= as_of, forward periods only ----
    est = f["earnings"]["estimates"]
    assert len(est) == 1                                   # backward + future-snapshot dropped
    assert est[0]["fiscal_period_end"] == "2026-12-31"
    assert est[0]["snapshot_date"] == "2025-12-30"         # newer of the two <= as_of
    assert est[0]["eps_estimate_avg"] == 13.28
    assert est[0]["eps_estimate_analysts"] == 46.0

    # ---- key stats + derived FCF yield ----
    ks = f["key_stats"]
    assert ks["beta_5y"] == 2.47
    assert ks["book_value_per_share"] == 39.55
    assert ks["revenue_ttm"] == 37454000000.0
    assert ks["fcf"] == 2600000000.0
    assert ks["fcf_yield_pct"] == 100.0 * 2600000000.0 / 800000000000.0
    assert ks["eps_estimate_current_year"] == 7.41
    assert ks["wall_street_target"] == 516.13


def test_missing_data_is_fail_soft(pg_session):
    s = pg_session
    iid = _instrument(s, "TSTB")
    # no fundamentals, no earnings, no estimates
    f = compute_financials(s, iid, "TSTB", date(2026, 1, 1))
    assert f["snapshot_as_of"] is None
    assert f["statements"]["income"]["annual"] == []
    assert f["earnings"]["history"] == []
    assert f["earnings"]["estimates"] == []
    assert f["key_stats"]["beta_5y"] is None
    assert f["key_stats"]["fcf_yield_pct"] is None
