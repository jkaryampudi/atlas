"""xsmom_run: the evaluation policy must be the one fixed in real_run
(imported, not re-declared), the family name must match the module SPEC, and
the binding benchmark is SPY — one registered trial, no sweeps (mirrors
test_candidate_run.py)."""
from atlas.dcp.backtest import real_run, xsmom_run
from atlas.dcp.signals.xsmom.v1 import SPEC


def test_evaluation_policy_is_imported_from_real_run():
    assert (xsmom_run.K_FOLDS, xsmom_run.HORIZON, xsmom_run.EMBARGO) == \
        (real_run.K_FOLDS, real_run.HORIZON, real_run.EMBARGO)
    assert xsmom_run.COSTS is real_run.COSTS
    assert xsmom_run.load_adjusted_obars is real_run.load_adjusted_obars


def test_family_name_matches_spec_and_benchmark_is_spy():
    assert SPEC["family"] == "xsmom"
    assert "no search" in str(SPEC["provenance"])
    assert xsmom_run.BENCHMARK == "SPY"
