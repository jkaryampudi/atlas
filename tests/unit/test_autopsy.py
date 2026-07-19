"""Fragility markers (autopsy.compute_autopsy): a HIMX-shaped fragile profile
fires the cluster of markers -> 'fragile'; a healthy name fires none -> 'clear';
a single overvaluation -> 'caution'; missing panels fail-soft to no markers.
"""
from __future__ import annotations

from atlas.dcp.research.autopsy import compute_autopsy

# HIMX (2026-07): bought ~$21.5 near its high, now $12.8; worth ~$7.9 on our
# earnings-based methods; Strong Sell; broken trend; thin ROE; shrinking revenue;
# expensive on earnings but cheap on book; huge vol.
_HIMX_MODELS = {
    "price": 12.8,
    "technical": {"summary": "Strong Sell", "sma_50": 17.6,
                  "bullish_signals": 1, "total_signals": 9},
    "momentum": {"mom_12_1": 0.77, "ret_20d": -0.245},
    "risk": {"vol_20d_ann": 0.857, "beta_vs_spy": 4.13},
    "quality": {"roe": 0.049},
}
_HIMX_VAL = {
    "price": 12.8,
    "summary": {"fair_value_central": 7.9},
    "dcf": {"historical_revenue_cagr": -0.013},
    "comparables": {"multiples": {"pe": {"percentile": 0.82},
                                  "ps": {"percentile": 0.21},
                                  "pb": {"percentile": 0.09}}},
    "dupont": {"roe": 0.049},
}


def test_fragile_profile_fires_the_cluster():
    a = compute_autopsy(_HIMX_MODELS, _HIMX_VAL)
    keys = {f["key"] for f in a["flags"]}
    assert keys == {"overvalued", "momentum_reversal", "broken_trend", "technical",
                    "low_quality", "shrinking", "margin_collapse", "high_volatility"}
    assert a["n_alerts"] == 5              # overvalued, reversal, broken_trend, technical, low_quality
    assert a["n_warns"] == 3               # shrinking, margin_collapse, high_volatility
    assert a["level"] == "fragile"
    # flags sorted most-severe first
    assert a["flags"][0]["severity"] == "alert"
    # a fired flag carries a concrete human reason
    ov = next(f for f in a["flags"] if f["key"] == "overvalued")
    assert "above Atlas's central fair value" in ov["detail"]


def test_healthy_profile_fires_nothing():
    models = {
        "price": 100.0,
        "technical": {"summary": "Buy", "sma_50": 95.0,
                      "bullish_signals": 7, "total_signals": 9},
        "momentum": {"mom_12_1": 0.20, "ret_20d": 0.05},
        "risk": {"vol_20d_ann": 0.25, "beta_vs_spy": 1.1},
        "quality": {"roe": 0.25},
    }
    val = {
        "price": 100.0,
        "summary": {"fair_value_central": 110.0},     # price BELOW our fair value
        "dcf": {"historical_revenue_cagr": 0.12},
        "comparables": {"multiples": {"pe": {"percentile": 0.4},
                                      "ps": {"percentile": 0.5},
                                      "pb": {"percentile": 0.5}}},
        "dupont": {"roe": 0.25},
    }
    a = compute_autopsy(models, val)
    assert a["flags"] == [] and a["level"] == "clear"
    assert a["n_alerts"] == 0 and a["n_warns"] == 0


def test_single_overvaluation_is_caution():
    models = {"price": 100.0, "technical": {"summary": "Neutral"},
              "momentum": {"mom_12_1": 0.1, "ret_20d": 0.02},
              "risk": {"vol_20d_ann": 0.3, "beta_vs_spy": 1.0},
              "quality": {"roe": 0.2}}
    val = {"price": 100.0, "summary": {"fair_value_central": 60.0},
           "dcf": {"historical_revenue_cagr": 0.05},
           "comparables": {"multiples": {"pe": {"percentile": 0.5},
                                         "ps": {"percentile": 0.5},
                                         "pb": {"percentile": 0.5}}},
           "dupont": {"roe": 0.2}}
    a = compute_autopsy(models, val)
    assert {f["key"] for f in a["flags"]} == {"overvalued"}
    assert a["level"] == "caution"


def test_fail_soft_on_missing_panels():
    assert compute_autopsy(None, None)["flags"] == []
    assert compute_autopsy(None, None)["level"] == "clear"
    assert compute_autopsy({}, {})["flags"] == []
