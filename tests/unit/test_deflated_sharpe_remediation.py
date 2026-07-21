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
