import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlas.dcp.risk.vol_target import MAX_GROSS, MAX_STEP, target_gross_exposure


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
