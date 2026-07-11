"""Stress testing (Doc 04 §7): scenario library v1 pinned verbatim, golden
scenario math validated against the hand calculations in the comments, and the
marginal broad-equity-crash policy gate. Every golden number below is derived
by hand from the §7 shock table — the doc requires stress math validated
against hand calculations."""
from decimal import Decimal

import pytest

from atlas.dcp.risk.engine import BreakerLevel, TradeProposal
from atlas.dcp.risk.stress import (
    SCENARIO_LIBRARY_V1,
    SCENARIOS_BY_KEY,
    STRESS_CRASH_LOSS_LIMIT,
    Contribution,
    StressHolding,
    run_library,
    run_scenario,
    stress_marginal_gate,
)

NAV = Decimal("100000")


def _holding(symbol="MSFT", value="10000", sector="Information Technology",
             india=False, ccy="USD", rate_beta="0") -> StressHolding:
    return StressHolding(symbol=symbol, value_aud=Decimal(value), sector_gics=sector,
                         india_exposed=india, currency=ccy,
                         rate_beta_per_100bp=Decimal(rate_beta))


# Golden book: NAV 100k, HWM 100k, four holdings (32k invested, 68k cash).
#   MSFT  10,000  Information Technology  US   USD
#   AEP    8,000  Utilities               US   USD  rate beta -0.04 per +100bp
#   INDA   8,000  Broad (India ETF)       IN   USD
#   NDIA   6,000  Broad (India ETF)       IN   AUD
BOOK = (
    _holding("MSFT", "10000", "Information Technology"),
    _holding("AEP", "8000", "Utilities", rate_beta="-0.04"),
    _holding("INDA", "8000", "Broad", india=True),
    _holding("NDIA", "6000", "Broad", india=True, ccy="AUD"),
)


def _run(key, holdings=BOOK, nav=NAV, hwm=NAV):
    return run_scenario(SCENARIOS_BY_KEY[key], holdings, nav_aud=nav,
                        high_water_mark=hwm)


# ---------------------------------------------------------- library is verbatim

def test_scenario_library_matches_doc04_s7_table_exactly():
    assert [s.key for s in SCENARIO_LIBRARY_V1] == [
        "broad_equity_crash", "rates_shock", "india_shock",
        "sector_collapse", "aud_spike", "liquidity_event"]
    crash = SCENARIOS_BY_KEY["broad_equity_crash"]
    assert crash.us_equity_shock == Decimal("-0.20")
    assert crash.india_equity_shock == Decimal("-0.25")
    rates = SCENARIOS_BY_KEY["rates_shock"]
    assert rates.rate_shock_bp == Decimal("150")
    india = SCENARIOS_BY_KEY["india_shock"]
    assert india.india_equity_shock == Decimal("-0.15")
    assert india.india_fx_shock == Decimal("-0.08")
    assert SCENARIOS_BY_KEY["sector_collapse"].largest_sector_shock == Decimal("-0.35")
    assert SCENARIOS_BY_KEY["aud_spike"].aud_appreciation == Decimal("0.10")
    liq = SCENARIOS_BY_KEY["liquidity_event"]
    assert liq.spread_multiple == Decimal("5")
    assert liq.fill_slippage == Decimal("-0.02")


# ------------------------------------------------------------- golden scenarios

def test_broad_equity_crash_golden():
    # Hand calc: US names -20%: MSFT 10,000 -> -2,000; AEP 8,000 -> -1,600.
    # India names -25%: INDA 8,000 -> -2,000; NDIA 6,000 -> -1,500.
    # Total -7,100 = -7.10% NAV; NAV after 92,900 vs HWM 100,000 -> dd -7.1% -> DD1.
    r = _run("broad_equity_crash")
    assert r.loss_aud == Decimal("-7100.00")
    assert r.nav_impact_pct == Decimal("-0.071000")
    assert r.breaker_after is BreakerLevel.DD1
    # top-3 contributors, most negative first; -2,000 tie broken alphabetically
    assert r.worst_contributors == (
        Contribution("INDA", Decimal("-2000.00")),
        Contribution("MSFT", Decimal("-2000.00")),
        Contribution("AEP", Decimal("-1600.00")),
    )


def test_rates_shock_golden_uses_holding_betas():
    # Hand calc: only AEP carries duration: -0.04 per +100bp x 150bp = -6%
    # -> 8,000 x -0.06 = -480. Zero-beta names are untouched and are NOT
    # listed as contributors.
    r = _run("rates_shock")
    assert r.loss_aud == Decimal("-480.00")
    assert r.nav_impact_pct == Decimal("-0.004800")
    assert r.breaker_after is BreakerLevel.NONE
    assert r.worst_contributors == (Contribution("AEP", Decimal("-480.00")),)


def test_india_shock_golden_compounds_equity_and_fx():
    # Hand calc: shocks compound multiplicatively: 0.85 x 0.92 - 1 = -0.218.
    # INDA 8,000 -> -1,744; NDIA 6,000 -> -1,308. Total -3,052 = -3.052% NAV.
    r = _run("india_shock")
    assert r.loss_aud == Decimal("-3052.00")
    assert r.nav_impact_pct == Decimal("-0.030520")
    assert r.worst_contributors == (
        Contribution("INDA", Decimal("-1744.00")),
        Contribution("NDIA", Decimal("-1308.00")),
    )


def test_sector_collapse_golden_hits_largest_non_broad_sector():
    # Hand calc: sector values — IT 10,000, Utilities 8,000; Broad ETFs are not
    # a sector bet (engine L3 convention). Largest = IT: 10,000 x -0.35 = -3,500.
    r = _run("sector_collapse")
    assert r.loss_aud == Decimal("-3500.00")
    assert r.nav_impact_pct == Decimal("-0.035000")
    assert r.worst_contributors == (Contribution("MSFT", Decimal("-3500.00")),)


def test_aud_spike_golden_translation_loss():
    # Hand calc: AUD +10% -> each non-AUD holding is worth 1/1.1 of its AUD
    # value: factor - 1 = -1/11 = -0.0909... Non-AUD book = 26,000
    # -> -2,363.6363... -> -2,363.64. NDIA is AUD-denominated: untouched.
    # MSFT -909.0909 -> -909.09; AEP/INDA -727.2727 -> -727.27 (tie: AEP first).
    r = _run("aud_spike")
    assert r.loss_aud == Decimal("-2363.64")
    assert r.nav_impact_pct == Decimal("-0.023636")
    assert r.worst_contributors == (
        Contribution("MSFT", Decimal("-909.09")),
        Contribution("AEP", Decimal("-727.27")),
        Contribution("INDA", Decimal("-727.27")),
    )


def test_liquidity_event_golden():
    # Hand calc: -2% fill slippage on the whole 32,000 book = -640.
    # MSFT -200, AEP -160, INDA -160, NDIA -120 (dropped: top 3 only;
    # AEP/INDA tie at -160 broken alphabetically).
    r = _run("liquidity_event")
    assert r.loss_aud == Decimal("-640.00")
    assert r.nav_impact_pct == Decimal("-0.006400")
    assert r.worst_contributors == (
        Contribution("MSFT", Decimal("-200.00")),
        Contribution("AEP", Decimal("-160.00")),
        Contribution("INDA", Decimal("-160.00")),
    )


# ------------------------------------------------------- distance-to-breaker

def test_distance_to_breaker_includes_existing_drawdown():
    # Already -10% off HWM (NAV 90k vs HWM 100k). Crash on a 40k US-only book:
    # -8,000 -> NAV after 82,000 -> dd vs HWM = -18% -> DD3.
    book = (_holding("SPY", "40000", "Broad"),)
    r = _run("broad_equity_crash", holdings=book, nav=Decimal("90000"),
             hwm=Decimal("100000"))
    assert r.loss_aud == Decimal("-8000.00")
    assert r.breaker_after is BreakerLevel.DD3


def test_empty_book_and_broad_only_book_have_no_sector_collapse():
    r = _run("sector_collapse", holdings=())
    assert r.loss_aud == Decimal("0.00")
    assert r.nav_impact_pct == Decimal("0.000000")
    assert r.breaker_after is BreakerLevel.NONE
    assert r.worst_contributors == ()
    broad = (_holding("SPY", "30000", "Broad"),)
    assert _run("sector_collapse", holdings=broad).loss_aud == Decimal("0.00")


def test_largest_sector_tie_breaks_alphabetically():
    # Utilities and Information Technology both 10,000: deterministic pick is
    # the alphabetically-first sector, Information Technology -> MSFT shocked.
    book = (_holding("MSFT", "10000", "Information Technology"),
            _holding("AEP", "10000", "Utilities"))
    r = _run("sector_collapse", holdings=book)
    assert r.worst_contributors == (Contribution("MSFT", Decimal("-3500.00")),)


def test_run_scenario_requires_positive_nav():
    with pytest.raises(ValueError, match="NAV"):
        _run("broad_equity_crash", nav=Decimal("0"))


def test_run_library_covers_all_six_scenarios_in_order():
    results = run_library(BOOK, nav_aud=NAV, high_water_mark=NAV)
    assert [r.key for r in results] == [s.key for s in SCENARIO_LIBRARY_V1]


# ------------------------------------------------------- §7 marginal gate

def _proposal(cost_aud: str, india: bool = False) -> TradeProposal:
    # qty x entry x fx = cost_aud (fx 1 keeps the arithmetic transparent)
    return TradeProposal(symbol="NEW", side="BUY", qty=1,
                         entry_price=Decimal(cost_aud), stop_price=Decimal("1"),
                         fx_to_aud=Decimal("1"), instrument_type="stock",
                         sector_gics="Information Technology",
                         india_exposed=india, currency="USD", adv_20d=1_000_000,
                         corr_with_existing={})


def test_stress_gate_fails_when_pro_forma_crash_loss_beyond_25pct():
    # Hand calc: existing 90k US book -> crash -18,000 (-18%). Proposal 40k
    # India-exposed adds -25% x 40,000 = -10,000 -> pro-forma -28% < -25% FAIL.
    book = (_holding("SPY", "90000", "Broad"),)
    r = stress_marginal_gate(_proposal("40000", india=True), book, nav_aud=NAV)
    assert r.rule == "STRESS" and not r.passed
    assert "-0.2800" in r.detail and "-0.1800" in r.detail  # marginal effect visible


def test_stress_gate_passes_within_limit_and_at_exact_boundary():
    book = (_holding("SPY", "90000", "Broad"),)
    # -18,000 - 20% x 20,000 = -22,000 -> -22% within the limit
    assert stress_marginal_gate(_proposal("20000"), book, nav_aud=NAV).passed
    # boundary: 100k US book (-20,000) + 20k India (-5,000) = exactly -25%.
    # "beyond -25%" (§7) is strict: exactly -25% passes.
    full = (_holding("SPY", "100000", "Broad"),)
    r = stress_marginal_gate(_proposal("20000", india=True), full, nav_aud=NAV)
    assert r.passed and STRESS_CRASH_LOSS_LIMIT == Decimal("-0.25")


def test_stress_gate_on_empty_book_uses_proposal_alone():
    # 30k US proposal on an all-cash book: -6,000 = -6% -> pass.
    r = stress_marginal_gate(_proposal("30000"), (), nav_aud=NAV)
    assert r.passed and "-0.0600" in r.detail and "0.0000" in r.detail
