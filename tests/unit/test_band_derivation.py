"""Percentile tolerance-band derivation (board item 7; ADR-0010/0013).

Every number asserted here is HAND-DERIVED in the comments — the goldens pin
the derivation math, the tighten-only rule, and the CUSUM parameterisation.

Golden A (default 126-session excess window, 327 sessions): the strategy
curve is CONSTRUCTED so every trailing-126-session window return is a chosen
value — c[t] = c[t-126] * (1 + w[t]) with c[0..125] = 1.0 — and SPY is flat,
so the excess distribution IS the chosen w list in pp. 201 overlapping
windows; three worst chosen as -30%, -20%, -10%; all others +5%. Type-7
percentile at q=0.01: pos = 0.01 * (201-1) = 2.0 exactly -> the 3rd-smallest
sorted value, -10.0 pp. No interpolation, no ambiguity.

Golden B (default 252-session DD window, 300 sessions): rise 1.0 -> 2.0 at
t=150, fall to 1.2 at t=200, flat after. Full max DD = 1.2/2 - 1 = -0.40;
every one of the 48 rolling 252-session windows contains the peak AND the
trough, so the window distribution is constant -0.40 and its 1st percentile
is -0.40. dd_floor = min(-0.40, -0.40) * 1.1 margin = -0.44.

Golden C (small windows, fully hand-checkable): excess_window=2, dd_window=4
over 11 sessions — every window return and drawdown worked out by hand in
the test body.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta

import pytest

from atlas.dcp.backtest.band_derivation import (
    CUSUM_H_SIGMA,
    CUSUM_K_SIGMA,
    DD_MARGIN,
    DD_WINDOW,
    PERCENTILE,
    curve_sha256,
    derive_proposed_bands,
    max_drawdown,
    rolling_max_dd_distribution,
    trailing_excess_distribution,
)
from atlas.dcp.trading.bands import DD_BAND_KEY, EXCESS_BAND_KEY, EXCESS_SESSIONS

PROVISIONAL = {  # the ADR-0010 provisional bands, verbatim
    "provisional": True,
    "demote_to": "suspended",
    DD_BAND_KEY: -0.40,
    EXCESS_BAND_KEY: -25.0,
}


def _dates(n: int) -> list[date]:
    return [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]


def _golden_a_curves() -> tuple[list[float], list[float]]:
    """327 sessions; window returns w[t] for t in 126..326: -30% at t=130,
    -20% at t=200, -10% at t=260, +5% everywhere else; SPY flat."""
    n = 327
    w = {t: 0.05 for t in range(126, n)}
    w[130], w[200], w[260] = -0.30, -0.20, -0.10
    c = [1.0] * 126
    for t in range(126, n):
        c.append(c[t - 126] * (1.0 + w[t]))
    return c, [1.0] * n


# ------------------------------------------------------------ distributions

def test_golden_a_excess_distribution_and_percentile():
    c, spy = _golden_a_curves()
    dist = trailing_excess_distribution(c, spy)
    assert len(dist) == 327 - EXCESS_SESSIONS == 201    # ALL overlapping windows
    # the three chosen bad windows, recovered exactly (in pp)
    assert sorted(dist)[:3] == pytest.approx([-30.0, -20.0, -10.0], rel=1e-12)
    d = derive_proposed_bands(
        dates=_dates(327), strategy_curve=c, spy_curve=spy,
        provisional=PROVISIONAL, curve_note="golden A")
    # q=0.01 over 201 values: pos = 2.0 exactly -> 3rd smallest = -10.0 pp
    assert d.derived_excess_floor_pp == pytest.approx(-10.0, rel=1e-12)
    assert d.excess_windows == 201


def test_golden_b_dd_floor_full_window_dominates():
    n = 300
    c = ([1.0 + t / 150 for t in range(151)]            # 1.0 -> 2.0 at t=150
         + [2.0 - 0.8 * (t - 150) / 50 for t in range(151, 201)]  # -> 1.2
         + [1.2] * 99)                                   # flat to t=299
    assert len(c) == n
    spy = [1.0 + t / 300 for t in range(n)]              # any positive curve
    assert max_drawdown(c) == pytest.approx(-0.40, rel=1e-12)
    dd_dist = rolling_max_dd_distribution(c)
    assert len(dd_dist) == n - DD_WINDOW == 48
    assert all(x == pytest.approx(-0.40, rel=1e-12) for x in dd_dist)
    d = derive_proposed_bands(
        dates=_dates(n), strategy_curve=c, spy_curve=spy,
        provisional=PROVISIONAL, curve_note="golden B")
    assert d.full_max_dd == pytest.approx(-0.40, rel=1e-12)
    assert d.rolling_dd_p1 == pytest.approx(-0.40, rel=1e-12)
    # margin x1.1 applied to the record: -0.40 * 1.1 = -0.44
    assert d.derived_dd_floor == pytest.approx(-0.40 * DD_MARGIN, rel=1e-12)
    assert DD_MARGIN == 1.1


# ------------------------------------------------- golden C: small windows

# c and flat SPY, 11 sessions. Hand-derived below (fractions in comments).
GOLDEN_C = [1.0, 1.1, 0.99, 1.05, 1.1, 1.2, 1.15, 1.25, 1.3, 1.2, 1.35]


def test_golden_c_small_window_hand_derivation():
    spy = [1.0] * 11
    # trailing-2 window returns, t = 2..10 (9 windows), in pp:
    #   t2: 0.99/1.0   - 1 = -1.0pp        t3: 1.05/1.1 - 1 = -1/22 = -4.5454..pp
    #   t9: 1.2/1.25   - 1 = -4.0pp        (all others positive)
    # sorted: [-4.5454.., -4.0, -1.0, ...]; q=0.01 over 9: pos = 0.08
    #   p1 = -100/22 + (-4.0 - (-100/22)) * 0.08 = -4.501818..pp
    dist = trailing_excess_distribution(GOLDEN_C, spy, window=2)
    assert len(dist) == 9
    lo = -100.0 / 22.0
    expected_p1 = lo + (-4.0 - lo) * 0.08
    # rolling 4-session max DD, starts i=0..6 (7 windows):
    #   w0 [1.0,1.1,0.99,1.05,1.1]  : 0.99/1.1 - 1 = -0.1
    #   w1 [1.1,0.99,1.05,1.1,1.2]  : -0.1
    #   w2..w4                       : 1.15/1.2 - 1 = -1/24
    #   w5 [1.2,1.15,1.25,1.3,1.2]  : 1.2/1.3 - 1 = -1/13
    #   w6 [1.15,1.25,1.3,1.2,1.35] : -1/13
    # sorted: [-0.1, -0.1, -1/13, -1/13, -1/24, -1/24, -1/24]
    # q=0.01 over 7: pos = 0.06 -> -0.1 + (-0.1 - -0.1)*0.06 = -0.1
    dd_dist = rolling_max_dd_distribution(GOLDEN_C, window=4)
    assert len(dd_dist) == 7
    assert sorted(dd_dist)[:2] == pytest.approx([-0.1, -0.1], rel=1e-12)
    # full-curve max DD: 0.99 from peak 1.1 -> -0.1 (worst anywhere)
    assert max_drawdown(GOLDEN_C) == pytest.approx(-0.1, rel=1e-12)

    d = derive_proposed_bands(
        dates=_dates(11), strategy_curve=GOLDEN_C, spy_curve=spy,
        provisional=PROVISIONAL, curve_note="golden C",
        excess_window=2, dd_window=4)
    assert d.derived_excess_floor_pp == pytest.approx(expected_p1, rel=1e-12)
    # dd_floor = min(full -0.1, rolling p1 -0.1) * 1.1 = -0.11
    assert d.derived_dd_floor == pytest.approx(-0.11, rel=1e-12)
    # CUSUM parameters: population stats of the daily strategy-minus-SPY
    # excess (SPY flat -> just the daily returns)
    rets = [GOLDEN_C[t] / GOLDEN_C[t - 1] - 1.0 for t in range(1, 11)]
    assert d.mean_daily_excess == pytest.approx(statistics.fmean(rets))
    assert d.sigma_daily_excess == pytest.approx(statistics.pstdev(rets))


# ---------------------------------------------------------- tighten-only

def test_tighten_only_stricter_derivation_replaces_provisional():
    spy = [1.0] * 11
    d = derive_proposed_bands(
        dates=_dates(11), strategy_curve=GOLDEN_C, spy_curve=spy,
        provisional=PROVISIONAL, curve_note="golden C",
        excess_window=2, dd_window=4)
    bands = d.tolerance_bands
    # derived excess -4.50..pp is STRICTER (closer to zero) than -25 -> replaced
    ex = d.decision(EXCESS_BAND_KEY)
    assert ex.tightened is True and bands[EXCESS_BAND_KEY] == ex.derived
    assert "STRICTER" in ex.note
    # derived dd -0.11 is STRICTER than -0.40 -> replaced
    dd = d.decision(DD_BAND_KEY)
    assert dd.tightened is True
    assert bands[DD_BAND_KEY] == pytest.approx(-0.11, rel=1e-12)
    assert bands["provisional"] is False
    assert bands["demote_to"] == "suspended"


def test_tighten_only_looser_derivation_keeps_provisional_verbatim():
    """A derivation LOOSER than the standing band never replaces it — the
    standing value is kept and the refusal recorded verbatim (loosening
    requires a new signed ADR)."""
    spy = [1.0] * 11
    tight = {**PROVISIONAL, DD_BAND_KEY: -0.05, EXCESS_BAND_KEY: -2.0}
    d = derive_proposed_bands(
        dates=_dates(11), strategy_curve=GOLDEN_C, spy_curve=spy,
        provisional=tight, curve_note="golden C",
        excess_window=2, dd_window=4)
    bands = d.tolerance_bands
    for key, standing in ((DD_BAND_KEY, -0.05), (EXCESS_BAND_KEY, -2.0)):
        dec = d.decision(key)
        assert dec.tightened is False and dec.chosen == standing
        assert bands[key] == standing                    # kept verbatim
        assert "LOOSEN" in dec.note and "signed ADR" in dec.note
    # the decisions land verbatim inside the proposed jsonb itself
    recorded = bands["derivation"]["decisions"]
    assert recorded[DD_BAND_KEY]["tightened"] is False
    assert recorded[DD_BAND_KEY]["provisional"] == -0.05
    assert "signed ADR" in recorded[DD_BAND_KEY]["note"]


def test_tighten_only_mixed_case():
    spy = [1.0] * 11
    mixed = {**PROVISIONAL, DD_BAND_KEY: -0.05, EXCESS_BAND_KEY: -25.0}
    d = derive_proposed_bands(
        dates=_dates(11), strategy_curve=GOLDEN_C, spy_curve=spy,
        provisional=mixed, curve_note="golden C",
        excess_window=2, dd_window=4)
    assert d.decision(DD_BAND_KEY).tightened is False        # -0.11 loosens -0.05
    assert d.tolerance_bands[DD_BAND_KEY] == -0.05
    assert d.decision(EXCESS_BAND_KEY).tightened is True     # -4.5 tightens -25
    assert d.tolerance_bands[EXCESS_BAND_KEY] == pytest.approx(-4.501818181818,
                                                               rel=1e-9)


# ------------------------------------------------------- derivation record

def test_derivation_record_is_embedded_and_reproducible():
    c, spy = _golden_a_curves()
    dates = _dates(327)
    d = derive_proposed_bands(dates=dates, strategy_curve=c, spy_curve=spy,
                              provisional=PROVISIONAL, curve_note="golden A")
    rec = d.tolerance_bands["derivation"]
    assert rec["percentile"] == PERCENTILE == 0.01
    assert rec["excess_windows"] == 201
    assert rec["dd_windows"] == 327 - DD_WINDOW
    assert rec["sessions"] == 327
    assert rec["window"] == f"{dates[0]}..{dates[-1]}"
    assert rec["curve_note"] == "golden A"
    assert rec["curve_sha256"] == curve_sha256(dates, c, spy)  # deterministic
    assert rec["dd_margin"] == DD_MARGIN
    cusum = d.tolerance_bands["cusum"]
    assert cusum["k_sigma"] == CUSUM_K_SIGMA == 0.5
    assert cusum["h_sigma"] == CUSUM_H_SIGMA == 5.0
    assert cusum["sigma_daily_excess"] > 0
    assert "page-only" in cusum["action_on_breach"]


# ------------------------------------------------------------- refusals

def test_refuses_mismatched_or_short_or_nonpositive_curves():
    spy = [1.0] * 11
    with pytest.raises(ValueError, match="same sessions"):
        derive_proposed_bands(dates=_dates(11), strategy_curve=GOLDEN_C[:-1],
                              spy_curve=spy, provisional=PROVISIONAL,
                              curve_note="x", excess_window=2, dd_window=4)
    with pytest.raises(ValueError, match="too short"):
        derive_proposed_bands(dates=_dates(2), strategy_curve=[1.0, 1.1],
                              spy_curve=[1.0, 1.0], provisional=PROVISIONAL,
                              curve_note="x", excess_window=2, dd_window=4)
    bad = list(GOLDEN_C)
    bad[5] = 0.0
    with pytest.raises(ValueError, match="positive"):
        derive_proposed_bands(dates=_dates(11), strategy_curve=bad,
                              spy_curve=spy, provisional=PROVISIONAL,
                              curve_note="x", excess_window=2, dd_window=4)


def test_refuses_zero_variance_excess_and_malformed_provisional():
    # identical curves -> every daily excess residual is 0 -> CUSUM cannot be
    # parameterised -> refuse loudly rather than emit a degenerate contract
    c = [1.0 + 0.01 * i for i in range(11)]
    with pytest.raises(ValueError, match="variance"):
        derive_proposed_bands(dates=_dates(11), strategy_curve=c,
                              spy_curve=list(c), provisional=PROVISIONAL,
                              curve_note="x", excess_window=2, dd_window=4)
    with pytest.raises(ValueError, match="provisional"):
        derive_proposed_bands(dates=_dates(11), strategy_curve=GOLDEN_C,
                              spy_curve=[1.0] * 11,
                              provisional={"demote_to": "suspended"},
                              curve_note="x", excess_window=2, dd_window=4)
    with pytest.raises(ValueError, match="provisional"):
        derive_proposed_bands(dates=_dates(11), strategy_curve=GOLDEN_C,
                              spy_curve=[1.0] * 11,
                              provisional={**PROVISIONAL, DD_BAND_KEY: 0.1},
                              curve_note="x", excess_window=2, dd_window=4)
