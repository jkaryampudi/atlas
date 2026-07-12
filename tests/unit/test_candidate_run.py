"""candidate_run: the evaluation policy must be the one fixed in real_run
(imported, not re-declared) and family naming in the trial registry must match
the module SPECs — one trial per (family, symbol), no sweeps."""
from atlas.dcp.backtest import candidate_run, real_run
from atlas.dcp.backtest.candidate_run import CANDIDATES


def test_evaluation_policy_is_imported_from_real_run():
    assert (candidate_run.WARMUP, candidate_run.K_FOLDS, candidate_run.HORIZON,
            candidate_run.EMBARGO) == (real_run.WARMUP, real_run.K_FOLDS,
                                       real_run.HORIZON, real_run.EMBARGO)
    assert (candidate_run.AVG_STOP_FRAC, candidate_run.AVG_TARGET_FRAC,
            candidate_run.TIME_STOP) == (real_run.AVG_STOP_FRAC,
                                         real_run.AVG_TARGET_FRAC,
                                         real_run.TIME_STOP)
    assert candidate_run.COSTS is real_run.COSTS
    assert candidate_run.load_adjusted_obars is real_run.load_adjusted_obars
    assert candidate_run.assert_symbol_data_clean is real_run.assert_symbol_data_clean


def test_family_names_match_specs():
    assert list(CANDIDATES) == ["trend", "meanrev", "breakout"]
    for family, (strategy, spec) in CANDIDATES.items():
        assert spec["family"] == family
        assert callable(strategy)
        assert "no search" in str(spec["provenance"])
