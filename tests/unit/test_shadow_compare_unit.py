"""Shadow comparison pure-unit surface: the 'shadow' budget sub-cap
resolution, the incumbent-default refusal (API and CLI, no DB touched), and
the report's honesty lines — the verdict must always say the harness is a
floor-check, never a ranking oracle, and the cost section must state what is
NOT attributable rather than estimate it. Pricing for claude-sonnet-5 itself
is pinned in tests/unit/test_pricing.py (known-model rate + fail-closed
unknown-model behavior)."""
from __future__ import annotations


import atlas.agents.shadow_compare as sc
from atlas.agents.runtime.registry import DEFAULT_MODEL
from atlas.agents.runtime.runner import surface_cap_usd


def test_shadow_surface_documented_default(monkeypatch):
    monkeypatch.delenv("ATLAS_BUDGET_SHADOW", raising=False)
    assert surface_cap_usd("shadow") == 3.00


def test_shadow_surface_env_override(monkeypatch):
    monkeypatch.setenv("ATLAS_BUDGET_SHADOW", "0.75")
    assert surface_cap_usd("shadow") == 0.75


def test_incumbent_default_resolution(monkeypatch):
    monkeypatch.delenv("ATLAS_MODEL_DEFAULT", raising=False)
    assert sc.incumbent_default() == DEFAULT_MODEL
    monkeypatch.setenv("ATLAS_MODEL_DEFAULT", "claude-opus-4-8")
    assert sc.incumbent_default() == "claude-opus-4-8"


def test_cli_refuses_incumbent_default_before_touching_anything(monkeypatch,
                                                                capsys):
    """The refusal fires before any DB connection or client construction —
    a self-comparison isolates nothing, so nothing may run or spend."""
    monkeypatch.delenv("ATLAS_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("ATLAS_DATABASE_URL", raising=False)   # proof: no DB needed
    assert sc.main(["--model", DEFAULT_MODEL]) == 2
    assert "REFUSED" in capsys.readouterr().err


def _comparison(*, halted: bool) -> sc.ShadowComparison:
    return sc.ShadowComparison(
        comparison_id="shadow-test", challenger_model="claude-sonnet-5",
        incumbent_models=(("cio", DEFAULT_MODEL),),
        question_hash="0" * 64,
        outcomes=(sc.ShadowOutcome("m1", "SHQX", "ok"),),
        incumbent_scores=(), challenger_scores=(),
        incumbent_cio_cost_usd={"m1": 0.02},
        challenger_cost_usd={"m1": 0.05}, halted=halted)


def test_report_verdict_is_a_floor_check_never_a_switch():
    report = sc.render_report(_comparison(halted=False))
    assert "FLOOR-CHECK, not a ranking oracle" in report
    assert "human read" in report
    assert "Principal-reviewed registry change" in report
    assert "NOTHING here switches any model" in report
    assert "PARTIAL RESULTS" not in report


def test_report_states_unattributable_incumbent_cost_honestly():
    report = sc.render_report(_comparison(halted=False))
    assert "CIO runs ONLY" in report
    assert "stated rather than estimated" in report


def test_report_flags_partial_results_when_halted():
    assert "PARTIAL RESULTS" in sc.render_report(_comparison(halted=True))
