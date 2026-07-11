"""Risk Engine (Doc 04): every rule L1-L11 exercised on both sides, breaker
latching, deterministic sizing — plus hypothesis properties proving no input
produces a size that violates any cap."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from atlas.dcp.risk.engine import (
    BreakerLevel,
    HoldingRisk,
    MIN_POSITION_AUD,
    PortfolioState,
    RuleResult,
    TradeProposal,
    computed_breaker,
    drawdown,
    limits_from_json,
    load_active_limit_set,
    next_breaker_state,
    size_position,
    validate,
)
from atlas.dcp.risk.vol_target import target_gross_exposure

SEED = json.loads((Path(__file__).parents[2] / "seeds" / "limit_set_v1.json").read_text())
LIMITS = limits_from_json(1, SEED["limits"])
NAV = Decimal("100000")


def _state(holdings=(), cash=None, new_today=0) -> PortfolioState:
    return PortfolioState(nav_aud=NAV, cash_aud=cash if cash is not None else NAV,
                          holdings=tuple(holdings), new_positions_today=new_today)


def _hold(symbol="MSFT", value="5000", sector="Information Technology",
          india=False, ccy="USD", risk="500") -> HoldingRisk:
    return HoldingRisk(symbol=symbol, value_aud=Decimal(value), sector_gics=sector,
                       india_exposed=india, currency=ccy, risk_to_stop_aud=Decimal(risk))


def _proposal(**kw) -> TradeProposal:
    base = dict(symbol="AVGO", side="BUY", qty=20, entry_price=Decimal("250"),
                stop_price=Decimal("245"), fx_to_aud=Decimal("1.5"),
                instrument_type="stock", sector_gics="Information Technology",
                india_exposed=False, currency="USD", adv_20d=1_000_000,
                corr_with_existing={})
    base.update(kw)
    return TradeProposal(**base)


def _rule(check, name) -> RuleResult:
    return next(r for r in check.results if r.rule == name)


# ------------------------------------------------------------------ validate

def test_clean_proposal_passes_all_rules():
    check = validate(_proposal(), _state(), LIMITS)
    assert check.passed and not check.failures()
    assert len(check.results) == 12  # DD + L1..L11 all itemised


def test_l1_stock_weight_breach():
    p = _proposal(qty=2200)  # 2200*250*1.5 = 825k... use nav-scaled: 2200 shares -> weight >8%
    p = _proposal(qty=25, entry_price=Decimal("250"))  # 9375 AUD = 9.4% > 8%
    check = validate(p, _state(), LIMITS)
    assert not _rule(check, "L1").passed and not check.passed
    assert _rule(check, "L2").passed  # n/a branch


def test_l1_counts_existing_holding_in_same_symbol():
    state = _state(holdings=[_hold(symbol="AVGO", value="5000")])
    p = _proposal(qty=10)  # 3750 + 5000 = 8750 = 8.75% > 8%
    assert not _rule(validate(p, state, LIMITS), "L1").passed


def test_l2_etf_gets_higher_cap():
    p = _proposal(instrument_type="etf", sector_gics="Broad", qty=35)  # 13125 = 13.1% < 15%
    check = validate(p, _state(), LIMITS)
    assert _rule(check, "L2").passed and _rule(check, "L1").passed
    p2 = _proposal(instrument_type="etf", sector_gics="Broad", qty=45)  # 16875 = 16.9% > 15%
    assert not _rule(validate(p2, _state(), LIMITS), "L2").passed


def test_l3_sector_exposure_pro_forma():
    state = _state(holdings=[_hold(value="22000")])  # IT already 22%
    p = _proposal(qty=10)  # +3750 -> 25.75% > 25%
    assert not _rule(validate(p, state, LIMITS), "L3").passed


def test_l3_broad_etf_is_not_a_sector_bet():
    state = _state(holdings=[_hold(value="24000")])
    p = _proposal(instrument_type="etf", sector_gics="Broad", qty=10)
    assert _rule(validate(p, state, LIMITS), "L3").passed


def test_l4_india_sleeve_with_lookthrough():
    state = _state(holdings=[_hold(symbol="INDA", value="28000", sector="Broad",
                                   india=True)])
    p = _proposal(symbol="INFY", india_exposed=True, qty=20)  # +7500 -> 35.5% > 30%
    assert not _rule(validate(p, state, LIMITS), "L4").passed
    p_us = _proposal(qty=5)  # non-India instrument unaffected by L4
    assert _rule(validate(p_us, state, LIMITS), "L4").passed


def test_l5_cash_floor():
    state = _state(cash=Decimal("24000"))  # 24% cash
    p = _proposal(qty=15)  # cost 5625 -> cash 18.4% < 20%
    assert not _rule(validate(p, state, LIMITS), "L5").passed


def test_l6_trade_risk_and_dd1_halving():
    p = _proposal(qty=200, stop_price=Decimal("246"))  # risk 200*4*1.5=1200 = 1.2% > 1%
    assert not _rule(validate(p, _state(), LIMITS), "L6").passed
    p_ok = _proposal(qty=100, stop_price=Decimal("246"))  # 600 = 0.6% <= 1%
    assert _rule(validate(p_ok, _state(), LIMITS), "L6").passed
    # DD1: cap halves to 0.5% -> the same 0.6% trade now fails
    assert not _rule(validate(p_ok, _state(), LIMITS, breaker=BreakerLevel.DD1),
                     "L6").passed


def test_l7_aggregate_open_risk():
    state = _state(holdings=[_hold(risk="5600")])  # 5.6% open risk
    p = _proposal(qty=100, stop_price=Decimal("246"))  # +0.6% -> 6.2% > 6%
    assert not _rule(validate(p, state, LIMITS), "L7").passed


def test_l8_correlation_concentration():
    state = _state(holdings=[_hold(symbol="MSFT", value="9000")])
    p = _proposal(qty=12, corr_with_existing={"MSFT": Decimal("0.9")})
    # 9% + 4.5% = 13.5% combined > 12% with corr 0.9 > 0.8
    assert not _rule(validate(p, state, LIMITS), "L8").passed
    # low correlation is fine at the same weights
    p_low = _proposal(qty=12, corr_with_existing={"MSFT": Decimal("0.5")})
    assert _rule(validate(p_low, state, LIMITS), "L8").passed
    # high correlation but small combined weight is fine
    state_small = _state(holdings=[_hold(symbol="MSFT", value="3000")])
    p_small = _proposal(qty=8, corr_with_existing={"MSFT": Decimal("0.9")})
    assert _rule(validate(p_small, state_small, LIMITS), "L8").passed


def test_l9_new_positions_per_day():
    check = validate(_proposal(), _state(new_today=2), LIMITS)
    assert not _rule(check, "L9").passed
    # adding to an EXISTING position is not a new position
    state = _state(holdings=[_hold(symbol="AVGO", value="3000")], new_today=2)
    assert _rule(validate(_proposal(qty=5), state, LIMITS), "L9").passed


def test_l10_liquidity_and_fail_closed_without_adv():
    assert not _rule(validate(_proposal(adv_20d=0), _state(), LIMITS), "L10").passed
    assert not _rule(validate(_proposal(qty=60, adv_20d=1000), _state(), LIMITS),
                     "L10").passed  # 60 > 5% of 1000
    assert _rule(validate(_proposal(qty=50, adv_20d=1000), _state(), LIMITS),
                 "L10").passed


def test_l11_non_aud_exposure():
    state = _state(holdings=[_hold(value="82000")])  # 82% USD
    p = _proposal(qty=12)  # +4.5% -> 86.5% > 85%
    assert not _rule(validate(p, state, LIMITS), "L11").passed
    p_aud = _proposal(symbol="NDIA", instrument_type="etf", sector_gics="Broad",
                      currency="AUD", fx_to_aud=Decimal("1"), qty=12,
                      india_exposed=True)
    assert _rule(validate(p_aud, state, LIMITS), "L11").passed


def test_dd2_dd3_block_new_positions():
    for lvl in (BreakerLevel.DD2, BreakerLevel.DD3):
        check = validate(_proposal(), _state(), LIMITS, breaker=lvl)
        assert not check.passed and not _rule(check, "DD").passed
    assert _rule(validate(_proposal(), _state(), LIMITS,
                          breaker=BreakerLevel.DD1), "DD").passed


def test_validate_requires_positive_nav():
    state = PortfolioState(nav_aud=Decimal("0"), cash_aud=Decimal("0"),
                           holdings=(), new_positions_today=0)
    with pytest.raises(ValueError, match="NAV"):
        validate(_proposal(), state, LIMITS)


# ------------------------------------------------------------------ breakers

def test_drawdown_and_computed_levels():
    assert computed_breaker(drawdown(Decimal("96000"), NAV)) is BreakerLevel.NONE
    assert computed_breaker(drawdown(Decimal("95000"), NAV)) is BreakerLevel.DD1
    assert computed_breaker(drawdown(Decimal("90000"), NAV)) is BreakerLevel.DD2
    assert computed_breaker(drawdown(Decimal("85000"), NAV)) is BreakerLevel.DD3
    with pytest.raises(ValueError):
        drawdown(NAV, Decimal("0"))


def test_dd2_dd3_latch_until_human_clears():
    # recovery does NOT clear DD2 without the dual-confirmed human action
    assert next_breaker_state(BreakerLevel.DD2, Decimal("-0.02")) is BreakerLevel.DD2
    assert next_breaker_state(BreakerLevel.DD3, Decimal("-0.02")) is BreakerLevel.DD3
    # escalation through a latch is allowed
    assert next_breaker_state(BreakerLevel.DD2, Decimal("-0.16")) is BreakerLevel.DD3
    # human clearance steps down to the computed level
    assert next_breaker_state(BreakerLevel.DD2, Decimal("-0.02"),
                              human_cleared=True) is BreakerLevel.NONE
    # DD1 is not latched
    assert next_breaker_state(BreakerLevel.DD1, Decimal("-0.01")) is BreakerLevel.NONE


@given(st.decimals(min_value="-0.5", max_value="0.2", places=3),
       st.sampled_from(list(BreakerLevel)))
def test_property_no_deescalation_without_human(dd, current):
    nxt = next_breaker_state(current, dd)
    sev = {BreakerLevel.NONE: 0, BreakerLevel.DD1: 1, BreakerLevel.DD2: 2,
           BreakerLevel.DD3: 3}
    if current in (BreakerLevel.DD2, BreakerLevel.DD3):
        assert sev[nxt] >= sev[current]


def test_vol_target_input_validation():
    with pytest.raises(ValueError, match="current_gross"):
        target_gross_exposure(current_gross=1.5, realised_vol=0.1, target_vol=0.1)
    with pytest.raises(ValueError, match="vols"):
        target_gross_exposure(current_gross=0.5, realised_vol=0.0, target_vol=0.1)


def test_vol_target_breaker_dominance_wiring():
    # engine breaker states plug straight into the vol-target scaler (§11)
    for lvl in (BreakerLevel.DD1, BreakerLevel.DD2, BreakerLevel.DD3):
        assert target_gross_exposure(current_gross=0.4, realised_vol=0.05,
                                     target_vol=0.11,
                                     breaker_level=lvl.value) <= 0.4


# -------------------------------------------------------------------- sizing

def test_sizing_golden_case():
    # NAV 100k, risk budget 1000 AUD; risk/share 5 USD * 1.5 = 7.5 AUD -> 133 shares;
    # weight cap 8% = 8000 AUD / 375 = 21 shares -> binding L1/L2
    d = size_position(nav_aud=NAV, entry_price=Decimal("250"), stop_price=Decimal("245"),
                      fx_to_aud=Decimal("1.5"), instrument_type="stock",
                      adv_20d=1_000_000, limits=LIMITS)
    assert d.accepted and d.qty == 21 and d.binding_constraint == "L1/L2"


def test_sizing_risk_budget_binding():
    d = size_position(nav_aud=NAV, entry_price=Decimal("100"), stop_price=Decimal("80"),
                      fx_to_aud=Decimal("1"), instrument_type="stock",
                      adv_20d=1_000_000, limits=LIMITS)
    # budget 1000 / 20 = 50 shares (5000 AUD = 5% < 8% cap)
    assert d.accepted and d.qty == 50 and d.binding_constraint == "L6"


def test_sizing_liquidity_binding_and_dd1():
    d = size_position(nav_aud=NAV, entry_price=Decimal("100"), stop_price=Decimal("80"),
                      fx_to_aud=Decimal("1"), instrument_type="stock",
                      adv_20d=600, limits=LIMITS)
    assert d.accepted and d.qty == 30 and d.binding_constraint == "L10"
    dd1 = size_position(nav_aud=NAV, entry_price=Decimal("100"), stop_price=Decimal("80"),
                        fx_to_aud=Decimal("1"), instrument_type="stock",
                        adv_20d=1_000_000, limits=LIMITS, breaker=BreakerLevel.DD1)
    assert dd1.qty == 25  # halved risk budget -> 500/20


def test_sizing_rejects_stop_at_or_above_entry():
    d = size_position(nav_aud=NAV, entry_price=Decimal("100"), stop_price=Decimal("100"),
                      fx_to_aud=Decimal("1"), instrument_type="stock",
                      adv_20d=1000, limits=LIMITS)
    assert not d.accepted and d.binding_constraint == "stop"


def test_sizing_rejects_below_minimum_position():
    d = size_position(nav_aud=Decimal("20000"), entry_price=Decimal("500"),
                      stop_price=Decimal("400"), fx_to_aud=Decimal("1"),
                      instrument_type="stock", adv_20d=1_000_000, limits=LIMITS)
    # budget 200 / 100 = 2 shares -> 1000 AUD < 2000 minimum
    assert not d.accepted and d.binding_constraint == "min_position"


def test_sizing_zero_when_rounding_to_lot():
    d = size_position(nav_aud=NAV, entry_price=Decimal("100"), stop_price=Decimal("80"),
                      fx_to_aud=Decimal("1"), instrument_type="stock",
                      adv_20d=1_000_000, limits=LIMITS, lot_size=100)
    assert not d.accepted and d.detail == "size rounds to zero"


def test_sizing_input_validation():
    with pytest.raises(ValueError):
        size_position(nav_aud=Decimal("0"), entry_price=Decimal("1"),
                      stop_price=Decimal("0.5"), fx_to_aud=Decimal("1"),
                      instrument_type="stock", adv_20d=1, limits=LIMITS)


# ------------------------------------------------- property: caps are absolute

@given(
    nav_c=st.integers(min_value=50_000_00, max_value=5_000_000_00),
    entry_c=st.integers(min_value=1_00, max_value=5_000_00),
    stop_pct=st.integers(min_value=1, max_value=99),
    fx_c=st.integers(min_value=10, max_value=1000),      # 0.10 .. 10.00
    adv=st.integers(min_value=0, max_value=10_000_000),
    itype=st.sampled_from(["stock", "etf", "adr"]),
    breaker=st.sampled_from([BreakerLevel.NONE, BreakerLevel.DD1]),
    lot=st.sampled_from([1, 10, 100]),
)
def test_property_no_size_violates_any_cap(nav_c, entry_c, stop_pct, fx_c, adv,
                                           itype, breaker, lot):
    nav = Decimal(nav_c) / 100
    entry = Decimal(entry_c) / 100
    stop = (entry * Decimal(stop_pct) / 100).quantize(Decimal("0.01"))
    fx = Decimal(fx_c) / 100
    d = size_position(nav_aud=nav, entry_price=entry, stop_price=stop,
                      fx_to_aud=fx, instrument_type=itype, adv_20d=adv,
                      limits=LIMITS, breaker=breaker, lot_size=lot)
    if not d.accepted:
        assert d.qty == 0
        return
    qty = Decimal(d.qty)
    # L6 risk cap (halved under DD1) is never exceeded
    assert qty * (entry - stop) * fx <= nav * LIMITS.risk_per_trade(breaker)
    # L1/L2 weight cap is never exceeded
    cap = LIMITS.l2_max_etf_weight if itype == "etf" else LIMITS.l1_max_stock_weight
    assert qty * entry * fx <= cap * nav
    # L10 liquidity cap is never exceeded
    assert qty <= LIMITS.l10_max_pct_adv * Decimal(adv)
    # minimum economic position holds
    assert qty * entry * fx >= MIN_POSITION_AUD
    assert d.qty % lot == 0


@given(
    entry_c=st.integers(min_value=10_00, max_value=1_000_00),
    stop_pct=st.integers(min_value=50, max_value=99),
    adv=st.integers(min_value=100_000, max_value=10_000_000),
    itype=st.sampled_from(["stock", "etf"]),
)
def test_property_engine_approved_size_validates_on_fresh_book(entry_c, stop_pct,
                                                               adv, itype):
    """The sizing function and the validator must agree: a size produced by §4
    on a fresh book always passes L1-L11."""
    entry = Decimal(entry_c) / 100
    stop = (entry * Decimal(stop_pct) / 100).quantize(Decimal("0.01"))
    d = size_position(nav_aud=NAV, entry_price=entry, stop_price=stop,
                      fx_to_aud=Decimal("1.5"), instrument_type=itype,
                      adv_20d=adv, limits=LIMITS)
    if not d.accepted:
        return
    p = TradeProposal(symbol="X", side="BUY", qty=d.qty, entry_price=entry,
                      stop_price=stop, fx_to_aud=Decimal("1.5"),
                      instrument_type=itype,
                      sector_gics="Broad" if itype == "etf" else "Information Technology",
                      india_exposed=False, currency="USD", adv_20d=adv,
                      corr_with_existing={})
    check = validate(p, _state(), LIMITS)
    assert check.passed, [r for r in check.results if not r.passed]


# ------------------------------------------------------------------- loaders

def test_limits_from_seed_json():
    assert LIMITS.l6_max_risk_per_trade == Decimal("0.01")
    assert LIMITS.l9_max_new_positions_per_day == 2
    assert LIMITS.risk_per_trade(BreakerLevel.DD1) == Decimal("0.005")


def test_load_active_limit_set_parses_json_string():
    class _Row:
        version = 1
        limits = json.dumps(SEED["limits"])

    class _Session:
        def execute(self, *a, **k):
            class _R:
                @staticmethod
                def first():
                    return _Row()
            return _R()

    lim = load_active_limit_set(_Session(), date(2026, 7, 14))
    assert lim.l1_max_stock_weight == Decimal("0.08")


def test_load_active_limit_set_fails_without_rows():
    class _Session:
        def execute(self, *a, **k):
            class _R:
                @staticmethod
                def first():
                    return None
            return _R()

    with pytest.raises(RuntimeError, match="no limit set"):
        load_active_limit_set(_Session(), date(2026, 7, 14))
