from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlas.dcp.risk.engine import BreakerLevel
from atlas.dcp.risk.vol_target import (
    MAX_GROSS,
    MAX_STEP,
    STEP_CAP,
    gross_step_gate,
    target_gross_exposure,
)

# The VOL gate's gross ceiling is passed in by the caller as (1 - active L5).
# Under limit_set v2 (L5=0.10) that is 0.90 (Principal 2026-07-18); v1 was 0.80.
CAP_V2 = Decimal("0.90")
CAP_V1 = Decimal("0.80")


def test_scales_down_in_high_vol():
    g = target_gross_exposure(current_gross=0.8, realised_vol=0.24, target_vol=0.12)
    assert g == pytest.approx(0.70)          # ideal 0.40 but step-bounded to -0.10


def test_scales_up_in_calm_but_capped_at_gross_max():
    g = target_gross_exposure(current_gross=0.78, realised_vol=0.06, target_vol=0.12)
    assert g == MAX_GROSS


def test_breakers_dominate_no_increase_under_dd2():
    g = target_gross_exposure(current_gross=0.5, realised_vol=0.06, target_vol=0.12,
                              breaker_level="DD2")
    assert g == 0.5


@given(cur=st.floats(min_value=0.0, max_value=1.0),
       rv=st.floats(min_value=0.01, max_value=1.0),
       tv=st.floats(min_value=0.01, max_value=0.5),
       br=st.sampled_from(["none", "DD1", "DD2", "DD3"]))
def test_invariants_hold_for_all_inputs(cur, rv, tv, br):
    g = target_gross_exposure(current_gross=cur, realised_vol=rv, target_vol=tv,
                              breaker_level=br)
    assert 0.0 <= g <= MAX_GROSS
    assert abs(g - cur) <= MAX_STEP + 1e-9 or g <= cur   # step bound (or breaker clamp)
    if br in ("DD1", "DD2", "DD3"):
        assert g <= cur                                   # never scales up in breaker states


# ------------------------------------------- gross_step_gate (§11 as a gate)

def test_gate_step_cap_is_the_derived_constant():
    """STEP_CAP is the §11 daily-step number, derived once from MAX_STEP; the
    gross ceiling is NOT a constant — it is passed in as (1 - active L5)."""
    assert STEP_CAP == Decimal(str(MAX_STEP)) == Decimal("0.10")


def test_gate_ceiling_tracks_l5_the_passed_cap_is_the_limit():
    """The ceiling TRACKS L5 (Principal 2026-07-18): the SAME gross fraction
    that fails against the v1 cap (0.80) passes against the v2 cap (0.90),
    and the reported limit is exactly the cap the caller supplied."""
    g = Decimal("0.8500")
    v1 = gross_step_gate(gross_after=g, step_after=Decimal("0.01"),
                         breaker=BreakerLevel.NONE, gross_cap=CAP_V1)
    v2 = gross_step_gate(gross_after=g, step_after=Decimal("0.01"),
                         breaker=BreakerLevel.NONE, gross_cap=CAP_V2)
    assert not v1.passed and "0.8500 > max 0.80" in v1.detail
    assert v2.passed and v2.limit == CAP_V2


def test_gate_passes_inside_all_bounds_with_exact_detail():
    r = gross_step_gate(gross_after=Decimal("0.0795"),
                        step_after=Decimal("0.0795"),
                        breaker=BreakerLevel.NONE, gross_cap=CAP_V2)
    assert (r.rule, r.passed) == ("VOL", True)
    assert r.detail == ("post-trade gross 0.0795 vs max 0.90 (= 1 - L5), "
                        "day gross increase 0.0795 vs max step 0.10, "
                        "breaker none")
    assert (r.value, r.limit) == (Decimal("0.0795"), CAP_V2)


def test_gate_boundaries_are_inclusive():
    """Exactly AT the caps passes — the §11 bounds are '<='."""
    assert gross_step_gate(gross_after=CAP_V2, step_after=STEP_CAP,
                           breaker=BreakerLevel.NONE, gross_cap=CAP_V2).passed


def test_gate_fails_past_max_gross():
    r = gross_step_gate(gross_after=Decimal("0.9001"),
                        step_after=Decimal("0.05"), breaker=BreakerLevel.NONE,
                        gross_cap=CAP_V2)
    assert not r.passed
    assert "BREACH: post-trade gross 0.9001 > max 0.90" in r.detail


def test_gate_fails_past_max_step():
    r = gross_step_gate(gross_after=Decimal("0.30"),
                        step_after=Decimal("0.1590"), breaker=BreakerLevel.NONE,
                        gross_cap=CAP_V2)
    assert not r.passed
    assert "BREACH: day gross increase 0.1590 > max step 0.10" in r.detail


def test_gate_allows_gross_increase_under_dd1():
    """Principal 2026-07-18, matching Doc 04 §5: DD1 = new-position risk
    HALVED (L6 -> 0.5%), entries still allowed. The VOL gate must NOT block a
    gross increase under DD1 — the L6 halving in engine.validate is the DD1
    remedy, not a gross freeze. (Regression pin for the collapse-into-DD2
    over-read the review flagged.)"""
    r = gross_step_gate(gross_after=Decimal("0.10"), step_after=Decimal("0.01"),
                        breaker=BreakerLevel.DD1, gross_cap=CAP_V2)
    assert r.passed
    assert "forbids gross increases" not in r.detail


@pytest.mark.parametrize("br", [BreakerLevel.DD2, BreakerLevel.DD3])
def test_dd2_dd3_forbid_gross_increases(br):
    """DD2 ('no new positions') and DD3 ('exit-only') forbid a gross increase
    even when both numeric bounds are comfortably met — defense-in-depth
    beside engine.validate's own DD gate."""
    r = gross_step_gate(gross_after=Decimal("0.10"), step_after=Decimal("0.01"),
                        breaker=br, gross_cap=CAP_V2)
    assert not r.passed
    assert f"breaker {br.value} forbids gross increases" in r.detail


def test_gate_itemises_every_breach_at_once():
    """Like engine.validate: a FAIL explains itself completely — all three
    breaches appear in one detail, no short-circuit."""
    r = gross_step_gate(gross_after=Decimal("0.95"),
                        step_after=Decimal("0.20"), breaker=BreakerLevel.DD2,
                        gross_cap=CAP_V2)
    assert not r.passed
    assert "breaker DD2 forbids gross increases" in r.detail
    assert "post-trade gross 0.9500 > max 0.90" in r.detail
    assert "day gross increase 0.2000 > max step 0.10" in r.detail
