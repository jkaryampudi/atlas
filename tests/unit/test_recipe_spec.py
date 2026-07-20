"""RecipeSpec grammar + factory catalog — fixture-only unit tests.

Pillars:
1. The v1 grammar is CLOSED: blank rationale refused, missing/blank lineage
   refused, LINEAGE BOUND to the rank_feature's FEATURE_LINEAGE declaration
   (ADR-0016 — the deflation count is never spec-chosen), unknown
   rank_feature refused, non-v1 vocabulary (direction / rebalance /
   universe / top_n bounds / costs, including float-typed costs) refused,
   year-suffixed names refused (the kill-family namespace), datetime
   kill_start refused, unknown spec keys refused — refusal, never coercion.
2. spec_hash is deterministic and canonical: golden literal pin, stability
   under mapping-order changes, sensitivity to every material field.
3. The feature catalog is BOUNDED: the pinned momentum grid, momentum_12_1
   served by IDENTITY with the phase-1 definition (never a twin), distinct
   names/specs per member, this module's source hashed into every family
   member's code_sha.
4. The fixed cost is THE committed CostModel — one source of truth.
5. --dry-run touches no database: the plan renders from the spec alone and
   a poisoned session factory proves nothing else is reached.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import date, datetime

import pytest

from atlas.dcp.backtest.real_run import COSTS
from atlas.dcp.factory.features import (
    FEATURE_LINEAGE,
    MOMENTUM_GRID,
    RANKABLE_FEATURES,
    family_member_name,
    get_rank_feature,
)
from atlas.dcp.factory.recipe_run import dry_run_plan, rebalance_superset
from atlas.dcp.factory.spec import (
    COST_BPS_PER_SIDE,
    GRAMMAR_VERSION,
    RecipeSpec,
    RecipeSpecError,
    spec_from_mapping,
)
from atlas.dcp.features.definitions import MOMENTUM_12_1
from tests.unit.test_impl_variant import weekdays

GOLDEN_RATIONALE = ("Winners keep winning: 12-1 cross-sectional momentum "
                    "persists (Jegadeesh-Titman 1993); the live book trades "
                    "its top-5.")


def make_spec(**overrides: object) -> RecipeSpec:
    base: dict[str, object] = dict(
        name="mom-12-1-top5", rank_feature="momentum_12_1", direction="desc",
        top_n=5, rebalance="monthly", universe="pit-sp500",
        lineage="momentum", rationale=GOLDEN_RATIONALE,
        kill_start=date(2016, 1, 1))
    base.update(overrides)
    return RecipeSpec(**base)  # type: ignore[arg-type]


# ------------------------------------------------- 1. the grammar is closed

def test_valid_spec_constructs_and_is_frozen():
    spec = make_spec()
    assert spec.family() == "recipe-mom-12-1-top5"
    assert spec.kill_family() == "recipe-mom-12-1-top5-2016"
    with pytest.raises(AttributeError):
        spec.top_n = 6  # type: ignore[misc]


@pytest.mark.parametrize("overrides,match", [
    ({"rationale": ""}, "rationale is required"),
    ({"rationale": "   "}, "rationale is required"),
    ({"lineage": ""}, "lineage is required"),
    ({"lineage": " "}, "lineage is required"),
    # ADR-0016 binding: a spec-chosen novel lineage would deflate at n=1 —
    # the renaming loophole migration 0032 closed, reopened one level up.
    ({"lineage": "six-month-persistence"}, "bound to lineage"),
    ({"lineage": "momentum "}, "bound to lineage"),          # typo channel
    ({"lineage": "Momentum"}, "bound to lineage"),           # case channel
    ({"rank_feature": "momentum_6_1", "lineage": "mom-6-1-line"},
     "bound to lineage"),
    ({"rank_feature": "sharpe_maximizer_9000"}, "unknown rank_feature"),
    ({"rank_feature": "sue_foster_olsen_shevlin"}, "unknown rank_feature"),
    ({"direction": "asc"}, "outside the v1 grammar"),
    ({"rebalance": "weekly"}, "outside the v1 grammar"),
    ({"universe": "pit-nifty50"}, "outside the v1 grammar"),
    ({"top_n": 0}, "outside the v1 bounds"),
    ({"top_n": 11}, "outside the v1 bounds"),
    ({"cost_bps_per_side": 0}, "never a free parameter"),
    ({"cost_bps_per_side": 5}, "never a free parameter"),
    # 10.0 == 10 but canonicalizes as '10.0' — a different spec_hash for a
    # semantically identical recipe; the type is part of the grammar.
    ({"cost_bps_per_side": 10.0}, "must be an int"),
    ({"cost_bps_per_side": True}, "must be an int"),
    ({"name": "Bad Name!"}, "must match"),
    ({"name": "ab"}, "must match"),
    # the kill-family namespace: 'x-2016' would collide with spec 'x''s
    # kill family 'recipe-x-2016' under one strategy_family string.
    ({"name": "mom-2016"}, "kill-family namespace"),
    ({"name": "alpha-1999"}, "kill-family namespace"),
    ({"kill_start": date(2012, 7, 1)}, "must be after"),
    ({"kill_start": date(2010, 1, 1)}, "must be after"),
    # datetime subclasses date; it would hash with a time component.
    ({"kill_start": datetime(2016, 1, 1)}, "plain date"),
])
def test_out_of_grammar_specs_refused(overrides, match):
    with pytest.raises(RecipeSpecError, match=match):
        make_spec(**overrides)


def test_year_like_but_not_kill_shaped_names_still_allowed():
    """Only a strict '-<19xx|20xx>' suffix is reserved: short numeric
    suffixes and interior years stay valid slugs."""
    assert make_spec(name="alpha-16").family() == "recipe-alpha-16"
    assert make_spec(name="mom-2016-v2").family() == "recipe-mom-2016-v2"


def test_mapping_unknown_and_missing_keys_refused():
    good = {
        "name": "mom-12-1-top5", "rank_feature": "momentum_12_1",
        "direction": "desc", "top_n": 5, "rebalance": "monthly",
        "universe": "pit-sp500", "lineage": "momentum",
        "rationale": GOLDEN_RATIONALE, "kill_start": "2016-01-01"}
    assert spec_from_mapping(good) == make_spec()
    with pytest.raises(RecipeSpecError, match="unknown spec key"):
        spec_from_mapping({**good, "leverage": 3})
    with pytest.raises(RecipeSpecError, match="missing spec key"):
        spec_from_mapping({k: v for k, v in good.items() if k != "lineage"})
    with pytest.raises(RecipeSpecError, match="not an ISO date"):
        spec_from_mapping({**good, "kill_start": "sometime in 2016"})
    with pytest.raises(RecipeSpecError, match="top_n must be an int"):
        spec_from_mapping({**good, "top_n": "5"})


# ----------------------------------------- 2. deterministic canonical hash

def test_spec_hash_golden_pin():
    """The exact canonical hash of the reference spec — any change to the
    canonicalization (field set, ordering rules, separators, grammar tag)
    breaks this literal and must arrive as a reviewed change."""
    assert make_spec().spec_hash() == (
        "661b9c060ee9c603c816e99654d8ce195468e68fea536ad169f3bed4c5ab5bd3")


def test_spec_hash_stable_and_sensitive():
    a, b = make_spec(), make_spec()
    assert a.spec_hash() == b.spec_hash()
    assert GRAMMAR_VERSION == "v1"
    # lineage no longer varies independently in-grammar (it is BOUND to the
    # rank_feature, ADR-0016) but it stays in the hashed payload: the
    # rank_feature change below moves the hash with the lineage pinned.
    for change in (
            {"name": "mom-12-1-topn"}, {"rank_feature": "momentum_6_1"},
            {"top_n": 6},
            {"rationale": GOLDEN_RATIONALE + " More."},
            {"kill_start": date(2017, 1, 1)}):
        assert make_spec(**change).spec_hash() != a.spec_hash(), change


def test_replace_revalidates():
    """dataclasses.replace re-runs __post_init__ — a frozen spec cannot be
    copied out of the grammar."""
    with pytest.raises(RecipeSpecError):
        replace(make_spec(), rationale="")


# ------------------------------------------------ 3. the bounded catalog

def test_momentum_grid_pinned_and_closed():
    assert MOMENTUM_GRID == ((252, 21), (126, 21), (63, 21), (252, 0))
    assert sorted(RANKABLE_FEATURES) == [
        "low_vol_252", "momentum_12_0", "momentum_12_1", "momentum_3_1",
        "momentum_6_1"]


def test_feature_lineage_binding_golden():
    """ADR-0016: every catalog member declares its lineage — the whole v1
    grid IS the momentum family, so every member binds to the exact string
    migration 0032 pinned. Coverage is total (the import-time guard in
    features.py refuses a member without a declared lineage, so widening the
    catalog forces the lineage declaration into the same reviewed diff)."""
    assert FEATURE_LINEAGE == {
        "momentum_12_1": "momentum",
        "momentum_6_1": "momentum",
        "momentum_3_1": "momentum",
        "momentum_12_0": "momentum",
        "low_vol_252": "low-vol",
    }
    assert set(FEATURE_LINEAGE) == set(RANKABLE_FEATURES)


def test_momentum_12_1_is_the_phase1_definition_by_identity():
    """Identity, not a twin: registering the catalog's canonical member can
    never collide with the phase-1 store registration."""
    assert RANKABLE_FEATURES["momentum_12_1"] is MOMENTUM_12_1


def test_family_members_pin_their_parameters_and_code():
    import atlas.dcp.factory.families.momentum as momentum_family
    for (lookback, skip) in MOMENTUM_GRID:
        name = family_member_name(lookback, skip)
        fd = RANKABLE_FEATURES[name]
        assert fd.spec["lookback_sessions"] == lookback
        assert fd.spec["skip_sessions"] == skip
        if name != "momentum_12_1":
            # the FAMILY module's own bytes are part of the pin (families/
            # restructure): widening the momentum grid re-hashes the momentum
            # members — and ONLY them; other families' pins are untouched
            assert str(momentum_family.__file__) in [
                str(p) for p in fd.code_paths]
    with pytest.raises(KeyError, match="reviewed change"):
        get_rank_feature("momentum_1_0")


def test_family_member_naming_convention():
    assert family_member_name(252, 21) == "momentum_12_1"
    assert family_member_name(126, 21) == "momentum_6_1"
    assert family_member_name(63, 21) == "momentum_3_1"
    assert family_member_name(252, 0) == "momentum_12_0"


# ------------------------------------------------- 4. the fixed-cost pin

def test_fixed_cost_is_the_committed_cost_model():
    assert COST_BPS_PER_SIDE == 10
    assert float(COST_BPS_PER_SIDE) == COSTS.commission_bps + COSTS.slippage_bps


# ------------------------------------------------------- 5. dry run only

def test_dry_run_plan_renders_from_spec_alone():
    plan = dry_run_plan(make_spec(), paths=1000, seed=7)
    assert "DRY RUN" in plan
    assert "no trial registered" in plan
    assert make_spec().spec_hash() in plan
    assert "recipe-mom-12-1-top5" in plan
    assert "recipe-mom-12-1-top5-2016" in plan
    assert "demote-only" in plan


def test_cli_dry_run_touches_no_database(monkeypatch, tmp_path, capsys):
    """--dry-run must be reachable without a database: the session factory
    is poisoned, so ANY DB access would fail the test."""
    import atlas.core.db as db
    from atlas.dcp.factory import recipe_run

    def _poisoned(*args: object, **kwargs: object) -> object:
        raise AssertionError("dry run touched the database")

    monkeypatch.setattr(db, "session_scope", _poisoned)
    spec_file = tmp_path / "recipe.json"
    spec_file.write_text(json.dumps({
        "name": "mom-12-1-top5", "rank_feature": "momentum_12_1",
        "direction": "desc", "top_n": 5, "rebalance": "monthly",
        "universe": "pit-sp500", "lineage": "momentum",
        "rationale": GOLDEN_RATIONALE, "kill_start": "2016-01-01"}))
    monkeypatch.setattr(sys, "argv",
                        ["recipe_run", "--spec", str(spec_file), "--dry-run"])
    recipe_run.main()
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "no trial registered" in out


def test_cli_refuses_bad_spec_before_anything_runs(monkeypatch, tmp_path):
    from atlas.dcp.factory import recipe_run
    spec_file = tmp_path / "recipe.json"
    spec_file.write_text(json.dumps({
        "name": "mom-12-1-top5", "rank_feature": "momentum_12_1",
        "direction": "desc", "top_n": 5, "rebalance": "monthly",
        "universe": "pit-sp500", "lineage": "momentum",
        "rationale": "", "kill_start": "2016-01-01"}))
    monkeypatch.setattr(sys, "argv",
                        ["recipe_run", "--spec", str(spec_file), "--dry-run"])
    with pytest.raises(RecipeSpecError, match="rationale is required"):
        recipe_run.main()


# ------------------------------------------- rebalance-superset structure

def test_rebalance_superset_covers_every_windows_rebalances():
    """month_end_indices over ANY (start, end) sub-window selects only
    sessions in rebalance_superset — the materialization target set covers
    the full run, the kill run and every walk-forward fold."""
    from atlas.dcp.backtest.portfolio import month_end_indices
    dates = weekdays(date(2024, 1, 1), 260)
    superset = set(rebalance_superset(dates))
    for start_i, end_i in ((0, len(dates)), (37, 190), (100, 101), (5, 63)):
        for t in month_end_indices(dates, start_i, end_i):
            assert dates[t] in superset
