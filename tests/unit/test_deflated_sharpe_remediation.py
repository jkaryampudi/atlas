"""F-005 numerical tests for the corrected Deflated Sharpe.

Verifies the two corrections against hand-computed reference values:
  1. the expected-maximum term scales by the supplied cross-trial dispersion
     (not the null-theoretical 1/sqrt(n_days) minimum), so a realistically larger
     dispersion LOWERS the DSR;
  2. the estimator variance carries the PSR skew/kurtosis term.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import pytest

from atlas.dcp.backtest.validation import deflated_sharpe

_ND = NormalDist()
_EULER = 0.5772156649


def _reference_dsr(sr_annual, n_days, n_trials, sigma_ann=None, skew=0.0, kurt=3.0):
    """Independent re-implementation of the BLdP DSR formula for cross-checking."""
    sr_d = sr_annual / math.sqrt(252)
    if n_trials <= 1:
        e_max = 0.0
    else:
        sigma = (sigma_ann / math.sqrt(252)) if sigma_ann is not None else math.sqrt(1.0 / n_days)
        e_max = sigma * ((1 - _EULER) * _ND.inv_cdf(1 - 1.0 / n_trials)
                         + _EULER * _ND.inv_cdf(1 - 1.0 / (n_trials * math.e)))
    est_var = (1 - skew * sr_d + (kurt - 1) / 4 * sr_d ** 2) / (n_days - 1)
    return _ND.cdf((sr_d - e_max) / math.sqrt(est_var))


def test_matches_independent_reference():
    for args in [(1.85, 1140, 1), (1.85, 1140, 68), (0.9, 800, 20)]:
        assert deflated_sharpe(*args) == pytest.approx(_reference_dsr(*args))


def test_empirical_dispersion_is_used_and_lowers_dsr():
    """A realistically larger cross-trial dispersion than the 1/sqrt(T) minimum
    raises the expected max and therefore LOWERS the DSR — the F-005 fix."""
    n_days = 1140
    minimum = math.sqrt(1.0 / n_days) * math.sqrt(252)   # the old default, annualised
    bigger = 3.0 * minimum
    dsr_min = deflated_sharpe(1.85, n_days, 68)                                  # fallback
    dsr_emp = deflated_sharpe(1.85, n_days, 68, sr_dispersion_annual=bigger)     # empirical
    assert dsr_emp < dsr_min
    assert dsr_emp == pytest.approx(_reference_dsr(1.85, n_days, 68, sigma_ann=bigger))


def test_supplying_the_minimum_dispersion_reproduces_the_fallback():
    n_days = 1140
    minimum_ann = math.sqrt(1.0 / n_days) * math.sqrt(252)
    assert deflated_sharpe(1.85, n_days, 68, sr_dispersion_annual=minimum_ann) == \
        pytest.approx(deflated_sharpe(1.85, n_days, 68))


def test_dispersion_below_floor_is_clamped_up_to_the_lower_bound():
    """The fail-closed floor (F-005): a supplied dispersion BELOW sqrt(1/n_days)
    is clamped up to it, so it reproduces the fallback and can NEVER raise the
    DSR above what the lower bound gives. The cross-trial std of Sharpe estimates
    cannot lie below the single-estimate SE, so this is the honest value, not a
    convenience — and it guarantees the gate is never loosened."""
    n_days = 1140
    minimum_ann = math.sqrt(1.0 / n_days) * math.sqrt(252)
    tiny = 0.1 * minimum_ann                              # a small-sample artefact
    clamped = deflated_sharpe(1.85, n_days, 68, sr_dispersion_annual=tiny)
    assert clamped == pytest.approx(deflated_sharpe(1.85, n_days, 68))   # == fallback
    # and it is never ABOVE the fallback for any dispersion, floored or not
    assert clamped <= deflated_sharpe(1.85, n_days, 68) + 1e-12


def test_worked_bldp_example_flagship_below_gate():
    """Worked BLdP example on the real flagship inputs (xsmom-pit-tr): annual
    Sharpe 0.821 over ~3524 daily bars, momentum lineage n_trials=23, empirical
    cross-trial dispersion 0.3257 (annual). Hand-computed against the independent
    reference; the honest DSR is ~0.75 — BELOW the 0.90 gate (consistent with the
    ADR-0018 research_shadow downgrade), and strictly below the lower-bound view."""
    sr, n_days, n_trials, disp = 0.821, 3524, 23, 0.3257
    honest = deflated_sharpe(sr, n_days, n_trials, sr_dispersion_annual=disp)
    lower_bound = deflated_sharpe(sr, n_days, n_trials)          # the old inflated view
    assert honest == pytest.approx(_reference_dsr(sr, n_days, n_trials, sigma_ann=disp))
    assert honest < lower_bound                                 # empirical > floor here
    assert honest < 0.90                                        # fails the gate, honestly
    assert honest == pytest.approx(0.752, abs=0.01)


def test_kurtosis_and_skew_move_the_denominator():
    base = deflated_sharpe(1.85, 1140, 1)
    fat_tails = deflated_sharpe(1.85, 1140, 1, kurtosis=9.0)      # more estimator variance
    assert fat_tails < base
    neg_skew = deflated_sharpe(1.85, 1140, 1, skew=-1.0)          # +variance -> lower DSR
    assert neg_skew < base


def test_monotone_in_trials_preserved():
    assert deflated_sharpe(1.85, 1140, 1) > deflated_sharpe(1.85, 1140, 68) > \
           deflated_sharpe(1.85, 1140, 500)


def test_short_sample_is_zero():
    assert deflated_sharpe(2.0, 20, 5) == 0.0
