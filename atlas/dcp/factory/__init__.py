"""Research Factory phase 1 (feature-store phase 2, Principal-approved
2026-07-18): a generic, SPEC-DRIVEN recipe gauntlet runner on the
point-in-time feature store.

Three modules, deliberately small:

  features.py   — the BOUNDED momentum feature family (registered
                  FeatureDefinitions computed with the production signal
                  math; no free-form formulas);
  spec.py       — RecipeSpec, the frozen, validated v1 recipe grammar with a
                  deterministic canonical spec_hash;
  recipe_run.py — spec -> register trial (BEFORE running) -> the full
                  portfolio gauntlet REUSED BY IMPORT from the committed
                  runners (xsmom_pit_run / impl_variant_run /
                  portfolio_validation) -> verdicts verbatim.

The load-bearing guarantee (pinned by tests/integration/test_recipe_run_pg),
stated with its scope: IN THE RANKING-COINCIDENT REGIME — the pin fixture,
where no ranked member pays a dividend, so the total-return transform is the
identity on every ranked series (the fixture ASSERTS this precondition; a
companion test proves it is load-bearing) — the recipe {rank momentum_12_1,
top 5, monthly, pit-sp500} reproduces the implementable-variant xsmom
gauntlet path BYTE-IDENTICALLY: same equity curve floats, same gate numbers,
same seeded monkey-null draws. On real dividend-paying data the two ranking
bases legitimately diverge: the recipe ranks on the store's price-basis
values — the LIVE generator's math, the DELIBERATE choice, because it is
what production actually trades — while impl_variant_run ranks on TR panel
closes, so a real-data recipe run is a DIFFERENT trial with its own verdict,
never a reproduction of xsmom-impl500-tr. That divergence is a basis
difference, not a bug on either side; do NOT "fix" the store toward TR
closes (it would break the store's genuine golden pin against the
production ranker, test_feature_equivalence_pg). "The store lies: fix the
store side, never the production side" applies ONLY to store-vs-generator
VALUE mismatches — a store that cannot serve the production math it claims
to carry.

Two-plane wall: pure DCP (core + dcp imports only); never touches
atlas/agents.
"""
