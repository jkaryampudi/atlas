"""RecipeSpec — the frozen, validated v1 recipe grammar (Research Factory
phase 1).

A recipe is the WHOLE experiment, named before it runs: what ranks the
universe, how many names are held, the pre-committed kill test, the lineage
whose multiple-testing penalty it inflates, and the economic rationale that
justifies burning a trial at all. No rationale, no run. No lineage, no run.

THE GRAMMAR IS DELIBERATELY BOUNDED. v1 exposes ONLY the knobs the existing
validated runners already exercise (xsmom_pit_run / impl_variant_run):

  name              slug ^[a-z0-9][a-z0-9-]{2,40}$ — names the trial families
                    (`recipe-<name>`, `recipe-<name>-<kill year>`). Names
                    ending in `-<year>` (-(19|20)\\d{2}) are REFUSED: they
                    are the kill-family namespace, and a spec named 'x-2016'
                    would collide with spec 'x''s kill family 'recipe-x-2016'
                    — one strategy_family string for two different
                    experiments (approval's registered-its-run check and the
                    evidence dashboard both key on family)
  rank_feature      a key of factory/features.RANKABLE_FEATURES (closed
                    momentum catalog; unknown names refused)
  direction         'desc' only (rank descending, higher is better — the
                    only direction any validated runner exercises)
  top_n             1..10 — bounded by the v1 winner floor (TOP_N=10); the
                    live book shape is 5 (SLEEVE_MAX_NAMES)
  rebalance         'monthly' only (the only validated schedule)
  universe          'pit-sp500' only (load_pit_panel: point-in-time
                    membership, fail-closed interval rule, delisted names
                    included, SPY outside the ranked universe)
  cost_bps_per_side FIXED at 10 (= the committed CostModel: 5 commission +
                    5 slippage) — costs are NOT a free parameter; a spec
                    naming any other number is refused
  lineage           REQUIRED, and BOUND (ADR-0016): must equal the
                    rank_feature's declared lineage in
                    factory/features.FEATURE_LINEAGE (every v1 member binds
                    to 'momentum'). The deflation count is never spec-chosen:
                    a spec-authored novel lineage would deflate at n=1 — the
                    exact renaming loophole ADR-0016 / migration 0032 closed,
                    reopened one level up. The field stays explicit (the
                    author states the line they burn a trial against, and the
                    spec_hash covers it) but validation REFUSES any value
                    other than the binding
  rationale         REQUIRED non-blank: the economic hypothesis, registered
                    verbatim as the trial's `hypothesis` — no economic
                    rationale, no run
  kill_start        REQUIRED pre-committed kill-trial start date (must be
                    after the membership-reliability bound WINDOW_START);
                    the kill trial can only demote, never validate

Unknown keys are refused. WIDENING THE GRAMMAR IS A REVIEWED CHANGE: any new
value/vocabulary/field lands here, in review, with the runner test evidence
that the widened grammar is exercised by a validated path — never as a
runtime escape hatch. Widening the FEATURE CATALOG additionally requires
declaring the new feature's lineage in FEATURE_LINEAGE in the same reviewed
diff (features.py refuses to import without it).

spec_hash: sha256 over the canonical JSON (sort_keys, compact separators,
default=str) of the full field set plus the grammar version — deterministic
across processes and stable under field reordering; pinned by a golden test.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Mapping

from atlas.dcp.factory.features import FEATURE_LINEAGE, RANKABLE_FEATURES
from atlas.dcp.market_data.index_membership import WINDOW_START

GRAMMAR_VERSION: Final[str] = "v1"
DIRECTIONS: Final[tuple[str, ...]] = ("desc",)
REBALANCES: Final[tuple[str, ...]] = ("monthly",)
UNIVERSES: Final[tuple[str, ...]] = ("pit-sp500",)
COST_BPS_PER_SIDE: Final[int] = 10          # fixed; never a free parameter
TOP_N_MIN: Final[int] = 1
TOP_N_MAX: Final[int] = 10                  # the v1 winner floor (TOP_N)
_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
# The kill-family namespace reservation (module docstring): kill_family() is
# 'recipe-<name>-<kill year>', so a NAME ending in '-<year>' would make one
# strategy_family string name two different experiments.
_YEAR_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"-(19|20)\d{2}$")

_FIELDS: Final[tuple[str, ...]] = (
    "name", "rank_feature", "direction", "top_n", "rebalance", "universe",
    "cost_bps_per_side", "lineage", "rationale", "kill_start")


class RecipeSpecError(ValueError):
    """A spec outside the bounded v1 grammar — refused, never coerced."""


@dataclass(frozen=True)
class RecipeSpec:
    """One validated recipe. Frozen: a spec cannot mutate after validation,
    so the spec_hash registered with the trial names exactly what ran."""

    name: str
    rank_feature: str
    direction: str
    top_n: int
    rebalance: str
    universe: str
    lineage: str
    rationale: str
    kill_start: date
    cost_bps_per_side: int = COST_BPS_PER_SIDE

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise RecipeSpecError(
                f"name {self.name!r} must match {_NAME_RE.pattern} (a slug: "
                "it names the registered trial families)")
        if _YEAR_SUFFIX_RE.search(self.name):
            raise RecipeSpecError(
                f"name {self.name!r} ends in '-<year>' — refused: "
                "'recipe-<name>-<year>' is the kill-family namespace "
                f"(kill_family()), and a spec named {self.name!r} would "
                "collide with another spec's kill trials under one "
                "strategy_family string")
        if self.rank_feature not in RANKABLE_FEATURES:
            raise RecipeSpecError(
                f"unknown rank_feature {self.rank_feature!r} — the v1 "
                f"catalog is the closed set {sorted(RANKABLE_FEATURES)}; "
                "widening it is a reviewed change")
        if self.direction not in DIRECTIONS:
            raise RecipeSpecError(
                f"direction {self.direction!r} is outside the v1 grammar "
                f"{DIRECTIONS} — widening it is a reviewed change")
        if not isinstance(self.top_n, int) or isinstance(self.top_n, bool):
            raise RecipeSpecError(f"top_n must be an int, got "
                                  f"{type(self.top_n).__name__}")
        if not TOP_N_MIN <= self.top_n <= TOP_N_MAX:
            raise RecipeSpecError(
                f"top_n {self.top_n} outside the v1 bounds "
                f"[{TOP_N_MIN}, {TOP_N_MAX}]")
        if self.rebalance not in REBALANCES:
            raise RecipeSpecError(
                f"rebalance {self.rebalance!r} is outside the v1 grammar "
                f"{REBALANCES} — widening it is a reviewed change")
        if self.universe not in UNIVERSES:
            raise RecipeSpecError(
                f"universe {self.universe!r} is outside the v1 grammar "
                f"{UNIVERSES} — widening it is a reviewed change")
        if (not isinstance(self.cost_bps_per_side, int)
                or isinstance(self.cost_bps_per_side, bool)):
            # 10.0 == 10 would pass the equality check below but canonicalize
            # as '10.0', silently changing the spec_hash of a semantically
            # identical recipe — spec_hash is the experiment's identity.
            raise RecipeSpecError(
                f"cost_bps_per_side must be an int, got "
                f"{type(self.cost_bps_per_side).__name__}")
        if self.cost_bps_per_side != COST_BPS_PER_SIDE:
            raise RecipeSpecError(
                f"cost_bps_per_side {self.cost_bps_per_side} refused — costs "
                f"are FIXED at {COST_BPS_PER_SIDE} bps/side (the committed "
                "CostModel), never a free parameter")
        if not self.lineage or not self.lineage.strip():
            raise RecipeSpecError(
                "lineage is required (ADR-0016): every recipe names the "
                "research line whose penalty it inflates")
        bound = FEATURE_LINEAGE[self.rank_feature]
        if self.lineage != bound:
            raise RecipeSpecError(
                f"lineage {self.lineage!r} refused — rank_feature "
                f"{self.rank_feature!r} is bound to lineage {bound!r} "
                "(FEATURE_LINEAGE, ADR-0016): the deflation count is never "
                "spec-chosen; a novel lineage would reset the "
                "multiple-testing penalty to n=1, the exact defect "
                "migration 0032 closed")
        if not self.rationale or not self.rationale.strip():
            raise RecipeSpecError(
                "rationale is required: no economic rationale, no run — the "
                "hypothesis is registered with the trial, before the result "
                "exists")
        if (not isinstance(self.kill_start, date)
                or isinstance(self.kill_start, datetime)):
            # datetime subclasses date: it would isoformat with a time
            # component (a different spec_hash for the same day) and crash
            # with a raw TypeError at the WINDOW_START comparison.
            raise RecipeSpecError("kill_start must be a plain date (the "
                                  "pre-committed kill-trial start)")
        if self.kill_start <= WINDOW_START:
            raise RecipeSpecError(
                f"kill_start {self.kill_start} must be after the "
                f"membership-reliability bound {WINDOW_START}")

    # ------------------------------------------------------------- identity

    def canonical(self) -> dict[str, object]:
        """The full field set plus the grammar version — the hashed payload
        and the registered trial-spec core."""
        return {
            "grammar": GRAMMAR_VERSION,
            "name": self.name,
            "rank_feature": self.rank_feature,
            "direction": self.direction,
            "top_n": self.top_n,
            "rebalance": self.rebalance,
            "universe": self.universe,
            "cost_bps_per_side": self.cost_bps_per_side,
            "lineage": self.lineage,
            "rationale": self.rationale,
            "kill_start": self.kill_start.isoformat(),
        }

    def spec_hash(self) -> str:
        """Deterministic canonical hash (module docstring; golden-pinned)."""
        payload = json.dumps(self.canonical(), sort_keys=True,
                             separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    # ------------------------------------------------------ trial families

    def family(self) -> str:
        return f"recipe-{self.name}"

    def kill_family(self) -> str:
        return f"recipe-{self.name}-{self.kill_start.year}"


def spec_from_mapping(raw: Mapping[str, object]) -> RecipeSpec:
    """Build a validated spec from a parsed JSON mapping (the CLI's --spec
    file). Unknown keys are refused — the grammar is closed, and a silently
    ignored knob is a lie about what ran."""
    unknown = sorted(set(raw) - set(_FIELDS))
    if unknown:
        raise RecipeSpecError(
            f"unknown spec key(s) {unknown} — the v1 grammar is exactly "
            f"{list(_FIELDS)}")
    missing = sorted(set(_FIELDS) - {"cost_bps_per_side"} - set(raw))
    if missing:
        raise RecipeSpecError(f"missing spec key(s) {missing}")
    kill_raw = raw["kill_start"]
    if isinstance(kill_raw, date):
        kill = kill_raw
    elif isinstance(kill_raw, str):
        try:
            kill = date.fromisoformat(kill_raw)
        except ValueError as exc:
            raise RecipeSpecError(
                f"kill_start {kill_raw!r} is not an ISO date") from exc
    else:
        raise RecipeSpecError("kill_start must be an ISO date string")

    def _s(key: str) -> str:
        v = raw[key]
        if not isinstance(v, str):
            raise RecipeSpecError(f"{key} must be a string, got "
                                  f"{type(v).__name__}")
        return v

    top_n = raw["top_n"]
    if not isinstance(top_n, int) or isinstance(top_n, bool):
        raise RecipeSpecError(f"top_n must be an int, got "
                              f"{type(top_n).__name__}")
    cost = raw.get("cost_bps_per_side", COST_BPS_PER_SIDE)
    if not isinstance(cost, int) or isinstance(cost, bool):
        raise RecipeSpecError(f"cost_bps_per_side must be an int, got "
                              f"{type(cost).__name__}")
    return RecipeSpec(
        name=_s("name"), rank_feature=_s("rank_feature"),
        direction=_s("direction"), top_n=top_n, rebalance=_s("rebalance"),
        universe=_s("universe"), lineage=_s("lineage"),
        rationale=_s("rationale"), kill_start=kill, cost_bps_per_side=cost)
