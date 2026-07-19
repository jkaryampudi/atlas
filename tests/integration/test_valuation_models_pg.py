"""Atlas's own valuation models (valuation_models.compute_valuation): the CAPM,
EPV, DCF, comparables and DuPont math verified against HAND-COMPUTED values on a
controlled synthetic company, plus the tax-normalisation and net-cash edges.
"""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy import text

from atlas.dcp.research.valuation_models import compute_valuation
from tests.conftest import requires_pg

pytestmark = requires_pg


def _instrument(s, sym, sector="TestTech"):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, currency, is_active, sector_gics) "
        "VALUES (:s,'US','US','stock',:s,'USD',true,:sec) RETURNING id"),
        {"s": sym, "sec": sector}).scalar()


def _fund(s, iid, as_of, payload):
    s.execute(text(
        "INSERT INTO market.fundamentals (instrument_id, as_of, payload, source) "
        "VALUES (:i,:d,CAST(:p AS jsonb),'EodhdAdapter')"),
        {"i": iid, "d": as_of, "p": json.dumps(payload)})


def _bar(s, iid, d, close):
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        " low, close, volume, source) VALUES (:i,:d,:c,:c,:c,:c,1000,'EodhdAdapter')"),
        {"i": iid, "d": d, "c": close})


def _peer(s, sym, pe, ev_ebitda, ev_ebit, ev_revenue, ps, pb, sector="TestTech"):
    iid = _instrument(s, sym, sector)
    _fund(s, iid, date(2025, 12, 31), {
        "Valuation": {"TrailingPE": str(pe), "EnterpriseValueEbitda": str(ev_ebitda),
                      "EnterpriseValueRevenue": str(ev_revenue),
                      "PriceSalesTTM": str(ps), "PriceBookMRQ": str(pb),
                      # EV/EBIT = EnterpriseValue / operatingIncome(1e9) = ev_ebit
                      "EnterpriseValue": str(ev_ebit * 1_000_000_000)},
        "Financials": {"Income_Statement": {"yearly": {
            "2025-12-31": {"date": "2025-12-31", "operatingIncome": "1000000000"}}}}})
    return iid


# A controlled company with round numbers so every model is hand-checkable.
_VALU = {
    "General": {"CurrencyCode": "USD"},
    "Technicals": {"Beta": "1.0"},
    "SharesStats": {"SharesOutstanding": "1000000000"},          # 1e9 shares
    "Highlights": {"RevenueTTM": "12000000000", "EBITDA": "3000000000",
                   "EarningsShare": "1.8", "BookValue": "10.0"},
    "Valuation": {"TrailingPE": "25", "EnterpriseValueEbitda": "15",
                  "EnterpriseValueRevenue": "4", "PriceSalesTTM": "4",
                  "PriceBookMRQ": "5", "EnterpriseValue": "48000000000"},
    "Financials": {
        "Income_Statement": {"yearly": {
            "2024-12-31": {"date": "2024-12-31", "totalRevenue": "10000000000",
                           "operatingIncome": "2000000000", "netIncome": "1500000000",
                           "incomeBeforeTax": "1800000000", "incomeTaxExpense": "360000000",
                           "interestExpense": "100000000", "ebitda": "2500000000"},
            "2025-12-31": {"date": "2025-12-31", "totalRevenue": "12000000000",
                           "operatingIncome": "2400000000", "netIncome": "1800000000",
                           "incomeBeforeTax": "2160000000", "incomeTaxExpense": "432000000",
                           "interestExpense": "100000000", "ebitda": "3000000000"},
        }},
        "Balance_Sheet": {"yearly": {
            "2025-12-31": {"date": "2025-12-31", "netDebt": "1000000000",
                           "totalAssets": "20000000000", "totalStockholderEquity": "10000000000",
                           "shortLongTermDebtTotal": "2000000000",
                           "cashAndShortTermInvestments": "1000000000"},
        }},
        "Cash_Flow": {"yearly": {
            "2025-12-31": {"date": "2025-12-31", "freeCashFlow": "2000000000"},
        }},
    },
}


def test_valuation_math_golden(pg_session):
    s = pg_session
    iid = _instrument(s, "VALU")
    _fund(s, iid, date(2025, 12, 31), _VALU)
    _bar(s, iid, date(2025, 12, 31), 50.0)                       # price 50, mktcap 5e10
    # sector peers (pe, ev_ebitda, ev_ebit, ev_revenue, ps, pb): medians land at
    # pe 20, ev_ebitda 10, ev_ebit 16, ev_revenue 2, ps 2, pb 2
    _peer(s, "PRA", 10, 8, 10, 1, 1, 1)
    _peer(s, "PRB", 20, 10, 16, 2, 2, 2)
    _peer(s, "PRC", 30, 12, 20, 3, 3, 3)

    v = compute_valuation(s, iid, "VALU", date(2026, 1, 1))

    assert v["price"] == 50.0 and v["net_debt"] == 1000000000.0

    # ---- cost of capital (hand) ----
    coc = v["cost_of_capital"]
    assert coc["assumptions"]["tax_rate"] == 0.20                # 432/2160
    assert abs(coc["cost_of_equity"] - 0.09) < 1e-12            # 0.04 + 1.0*0.05
    assert abs(coc["cost_of_debt_pretax"] - 0.05) < 1e-12       # 100/2000
    assert abs(coc["cost_of_debt_aftertax"] - 0.04) < 1e-12     # 0.05*(1-0.20)
    # WACC = (5e10/5.2e10)*0.09 + (0.2e10/5.2e10)*0.04
    wacc = (5e10 / 5.2e10) * 0.09 + (0.2e10 / 5.2e10) * 0.04
    assert abs(coc["wacc"] - wacc) < 1e-12

    # ---- EPV (hand) ----
    epv = v["epv"]
    assert abs(epv["normalized_operating_margin"] - 0.20) < 1e-12
    assert epv["normalized_ebit"] == 0.20 * 12e9               # 2.4e9
    assert abs(epv["nopat"] - 1.92e9) < 1.0                    # 2.4e9*0.8
    exp_epv_fv = (1.92e9 / wacc - 1e9) / 1e9
    assert abs(epv["fair_value_per_share"] - exp_epv_fv) < 1e-6
    assert abs(exp_epv_fv - 20.80) < 0.02                      # sanity: ~20.80

    # ---- DCF (structure + monotonicity; central > 0) ----
    dcf = v["dcf"]
    # base is UNLEVERED FCFF = vendor FCF + after-tax interest add-back
    assert dcf["levered_fcf"] == 2e9                           # vendor CFO - Capex
    assert abs(dcf["base_fcf"] - (2e9 + 0.1e9 * (1 - 0.20))) < 1.0   # 2.08e9 FCFF
    assert dcf["forecast_years"] == 5                          # horizon surfaced
    assert abs(dcf["historical_revenue_cagr"] - 0.20) < 1e-9   # (12/10)^1 - 1
    assert dcf["central_growth"] == 0.15                       # capped at GROWTH_CAP
    grid = dcf["sensitivity"]
    assert len(grid) == 12                                     # 4 growth x 3 wacc
    # at fixed wacc, higher growth => higher fair value
    at_base_wacc = sorted([g for g in grid if abs(g["wacc"] - wacc) < 1e-12],
                          key=lambda g: g["growth"])
    fvs = [g["fair_value_per_share"] for g in at_base_wacc]
    assert fvs == sorted(fvs)                                  # monotonic increasing
    # at fixed growth, higher wacc => lower fair value
    g15 = sorted([g for g in grid if g["growth"] == 0.15], key=lambda g: g["wacc"])
    assert [g["fair_value_per_share"] for g in g15] == sorted(
        [g["fair_value_per_share"] for g in g15], reverse=True)
    assert dcf["fair_value_per_share"] > 0

    # ---- DCF: 6 variants ({5y,10y} horizon x {growth,ebitda,revenue} terminal) ----
    assert set(dcf["variants"]) == {"5y_growth", "5y_ebitda", "5y_revenue",
                                    "10y_growth", "10y_ebitda", "10y_revenue"}
    assert dcf["exit_ev_ebitda"] == 15 and dcf["exit_ev_revenue"] == 4
    # the primary (feeds summary/verdict/autopsy) is the 5y perpetuity-growth variant
    assert dcf["fair_value_per_share"] == dcf["variants"]["5y_growth"]["fair_value_per_share"]
    assert all(vv["fair_value_per_share"] > 0 for vv in dcf["variants"].values())

    # ---- comparables (hand) — 6 multiples ----
    comps = v["comparables"]
    assert comps["n_peers"] == 3
    m = comps["multiples"]
    assert m["pe"]["peer_median"] == 20 and m["pe"]["implied_value"] == 20 * 1.8   # 36
    assert m["ev_ebitda"]["implied_value"] == (10 * 3e9 - 1e9) / 1e9               # 29
    assert m["ev_ebit"]["peer_median"] == 16                                        # [10,16,20]
    assert m["ev_ebit"]["implied_value"] == (16 * 2.4e9 - 1e9) / 1e9               # 37.4 (EBIT 2.4e9)
    assert m["ev_revenue"]["peer_median"] == 2                                      # [1,2,3]
    assert m["ev_revenue"]["implied_value"] == (2 * 12e9 - 1e9) / 1e9             # 23
    assert m["ps"]["implied_value"] == 2 * 12e9 / 1e9                              # 24
    assert m["pb"]["implied_value"] == 2 * 10.0                                    # 20
    assert abs(m["pe"]["percentile"] - 2 / 3) < 1e-9                               # 25 > 10,20
    # blended = median[36, 29, 37.4, 23, 24, 20] = (24+29)/2
    assert comps["blended_fair_value"] == 26.5

    # ---- DuPont (hand, latest year) ----
    dp = v["dupont"]
    assert abs(dp["net_margin"] - 0.15) < 1e-12                # 1.8/12
    assert abs(dp["asset_turnover"] - 0.6) < 1e-12            # 12/20
    assert abs(dp["equity_multiplier"] - 2.0) < 1e-12        # 20/10
    assert abs(dp["roe"] - 0.18) < 1e-12                     # 0.15*0.6*2.0

    # ---- summary range + verdict ----
    sm = v["summary"]
    assert len(sm["methods"]) == 3
    assert sm["fair_value_central"] == 26.5                  # median of the 3 centrals
    # price 50 sits between EPV ~20.8 and DCF ~53.8 -> within
    assert sm["verdict"] == "within our model range"


def test_tax_normalized_and_net_cash(pg_session):
    s = pg_session
    iid = _instrument(s, "EDGE")
    payload = json.loads(json.dumps(_VALU))
    # negative tax expense (a benefit) => effective rate negative => normalise to statutory
    payload["Financials"]["Income_Statement"]["yearly"]["2025-12-31"]["incomeTaxExpense"] = "-100000000"
    payload["Financials"]["Income_Statement"]["yearly"]["2025-12-31"]["incomeBeforeTax"] = "2000000000"
    # net CASH (negative net debt) should ADD to equity value
    payload["Financials"]["Balance_Sheet"]["yearly"]["2025-12-31"]["netDebt"] = "-5000000000"
    _fund(s, iid, date(2025, 12, 31), payload)
    _bar(s, iid, date(2025, 12, 31), 50.0)

    v = compute_valuation(s, iid, "EDGE", date(2026, 1, 1))
    assert v["cost_of_capital"]["assumptions"]["tax_rate"] == 0.21   # statutory fallback
    assert v["net_debt"] == -5000000000.0
    # equity = EV - net_debt = EV + 5e9  => equity strictly above enterprise value
    assert v["epv"]["equity_value"] > v["epv"]["enterprise_value"]


def test_negative_metrics_excluded_from_comps(pg_session):
    s = pg_session
    iid = _instrument(s, "LOSS")
    payload = json.loads(json.dumps(_VALU))
    payload["Highlights"]["EarningsShare"] = "-2.0"     # loss-making
    payload["Highlights"]["BookValue"] = "-5.0"         # negative book (deficit/buybacks)
    _fund(s, iid, date(2025, 12, 31), payload)
    _bar(s, iid, date(2025, 12, 31), 50.0)
    _peer(s, "QA", 10, 8, 10, 1, 1, 1)
    _peer(s, "QB", 20, 10, 16, 2, 2, 2)
    _peer(s, "QC", 30, 12, 20, 3, 3, 3)

    v = compute_valuation(s, iid, "LOSS", date(2026, 1, 1))
    m = v["comparables"]["multiples"]
    # negative implied values are still SHOWN for transparency ...
    assert m["pe"]["implied_value"] == 20 * -2.0        # -40
    assert m["pb"]["implied_value"] == 2 * -5.0         # -10
    # ... but excluded from the blend, which uses only the positive multiples
    assert v["comparables"]["blended_fair_value"] == 26.5   # median[29, 37.4, 23, 24]
    assert v["comparables"]["blended_fair_value"] > 0


def test_ev_multiples_require_positive_metric_even_with_net_cash(pg_session):
    s = pg_session
    iid = _instrument(s, "LOSSC")
    payload = json.loads(json.dumps(_VALU))
    # loss-making operating metrics: negative EBIT and negative EBITDA ...
    payload["Financials"]["Income_Statement"]["yearly"]["2025-12-31"]["operatingIncome"] = "-500000000"
    payload["Highlights"]["EBITDA"] = "-1000000000"
    # ... but NET CASH (negative net debt): without a positivity guard the bridge
    # (median * negative_metric - negative_net_debt)/shares launders a spurious
    # positive comp (e.g. EV/EBIT -> +12) into the blend.
    payload["Financials"]["Balance_Sheet"]["yearly"]["2025-12-31"]["netDebt"] = "-20000000000"
    _fund(s, iid, date(2025, 12, 31), payload)
    _bar(s, iid, date(2025, 12, 31), 50.0)
    _peer(s, "RA", 10, 8, 16, 2, 2, 2)
    _peer(s, "RB", 20, 10, 16, 2, 2, 2)
    _peer(s, "RC", 30, 12, 16, 2, 2, 2)

    v = compute_valuation(s, iid, "LOSSC", date(2026, 1, 1))
    m = v["comparables"]["multiples"]
    # negative EBIT / EBITDA -> the EV multiples are excluded, NOT laundered
    # positive by the net-cash bridge
    assert m["ev_ebit"]["implied_value"] is None
    assert m["ev_ebitda"]["implied_value"] is None
    assert v["net_debt"] == -20000000000.0


def test_net_debt_fail_soft_when_leg_missing(pg_session):
    s = pg_session
    iid = _instrument(s, "NDBT")
    payload = json.loads(json.dumps(_VALU))
    del payload["Financials"]["Balance_Sheet"]["yearly"]["2025-12-31"]["netDebt"]
    del payload["Financials"]["Balance_Sheet"]["yearly"]["2025-12-31"]["cashAndShortTermInvestments"]
    _fund(s, iid, date(2025, 12, 31), payload)
    _bar(s, iid, date(2025, 12, 31), 50.0)
    v = compute_valuation(s, iid, "NDBT", date(2026, 1, 1))
    assert v["net_debt"] is None                        # not fabricated to a debt-only figure
    assert v["epv"]["fair_value_per_share"] is None      # methods needing net debt fail-soft
    assert v["dcf"]["fair_value_per_share"] is None


def test_no_peers_and_fail_soft(pg_session):
    s = pg_session
    # lone name in a sector with no peers: comparables empty, other methods still run
    iid = _instrument(s, "SOLO", sector="EmptySector")
    _fund(s, iid, date(2025, 12, 31), _VALU)
    _bar(s, iid, date(2025, 12, 31), 50.0)
    v = compute_valuation(s, iid, "SOLO", date(2026, 1, 1))
    assert v["comparables"]["n_peers"] == 0
    assert v["comparables"]["blended_fair_value"] is None
    assert v["epv"]["fair_value_per_share"] is not None            # EPV still computes
    assert "EPV (no-growth floor)" in v["summary"]["methods"]

    # no fundamentals at all -> everything fail-soft to None/empty
    bare = _instrument(s, "BARE", sector="EmptySector")
    vb = compute_valuation(s, bare, "BARE", date(2026, 1, 1))
    assert vb["snapshot_as_of"] is None
    assert vb["epv"]["fair_value_per_share"] is None
    assert vb["summary"]["methods"] == []
    assert vb["summary"]["verdict"] is None
