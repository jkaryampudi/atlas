# RECIPE GAUNTLET — `recipe-mom-12-0-top5` (momentum_12_0, top-5, monthly, pit-sp500)

> ## WHAT THIS IS
> A spec-driven run of the committed portfolio gauntlet (Research Factory v1).
> Everything is imported from the validated runners — engine, eligibility, null
> model, thresholds, walk-forward, delisting rule — and the ranking values come
> from the point-in-time FEATURE STORE, whose equivalence to the production
> signal math is golden-pinned. Verdicts land verbatim, pass or fail.

## The spec (frozen; registered verbatim with both trials)

- spec_hash: `610ccd90facebc7fb23d0e33be494fd9309f5e2ed49fee63b77ef12134f9c90b`
- name: `mom-12-0-top5`; rank_feature: `momentum_12_0`; direction: desc; top_n: 5; rebalance: monthly; universe: pit-sp500
- costs: 10 bps/side, FIXED (the committed CostModel — never a free parameter)
- lineage: `momentum` (ADR-0016 — the deflation count basis)
- rationale (registered as the trial hypothesis, pre-run): Skip-month ablation, pre-registered: the canonical 12-1 skips the most recent month to dodge short-term reversal (Jegadeesh 1990). This spec includes that month (12-0) to test whether the skip is load-bearing on the modern point-in-time S&P 500 panel. Expectation from the literature is DEGRADATION versus 12-1; the experiment is registered because the expectation deserves a counted test, not an assumption -- a PASS would challenge the reversal premise on this panel. Registered before the result exists.
- pre-committed kill start: 2016-01-01 (demote-only)
- dataset_version: `d0cccbb08a5a9cac45c2ddf5616eadb86d95ed036b7b429d384de4ea0ce81172` (the feature store's input-vintage pin)
- ranking basis: stored feature values: split-adjusted PRICE closes — the production signal generator's basis (features/momentum.py, equivalence-pinned); accounting and benchmark are total-return per ADR-0009
- return convention: total return (split-adjusted; each dividend reinvested at its ex-date close — market_data/total_return.py)

## Panel and coverage (loader inherited unchanged)

- Panel 2010-01-04 → 2026-07-17; members with usable series: 657 (70 delisted); missing series: 3; SPY carries 66 reinvested distributions (asserted non-zero)
- Feature materialization: 198 rebalance sessions, 104299 values inserted, 0 already present, 0 failures (fail-loud)
- Null model: 1000-path seeded monkey MC (ADR-0002 #2); walk-forward k=4, horizon=40, embargo=10 (real_run constants, ADR-0002 #3)

## Trial 1 — `recipe-mom-12-0-top5`: the recipe on its full window

Family `recipe-mom-12-0-top5`; evaluation start 2012-07-02; 168 rebalances; forced delisting liquidations 0; unfilled buys 0.

Return +1625.01%, Sharpe 0.76, max drawdown -53.85%, avg turnover 69.68% per rebalance (sum |Δw|, both sides)

### Gate verdict: **FAIL**

- verdict: **FAIL**
- strategy TOTAL return: +1625.01%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +583.17%
- margin over SPY TR: +1041.84%
- equal-weight all-eligible TR (informational, NOT binding): +511.27%
- null-model p-value: 0.000 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.818 at n_trials=22 (lineage 'momentum', 22 registered trials; must be >= 0.9)
- trial registry id: `a0d1af37-6cde-48e8-b1b5-d01d9de99885` (registered and COMMITTED before the run; metrics enriched on the same row after)

Verbatim gate reasons:
- deflated Sharpe 0.82 < 0.9 at n_trials=22

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +122.72% | +55.66% | +67.06% |
| 2 | +26.86% | +64.96% | -38.10% |
| 3 | +69.07% | +34.94% | +34.13% |
| 4 | +267.22% | +91.66% | +175.56% |

- mean return +121.47%, mean Sharpe 0.79, worst fold +26.86%

### Exhibit: verdict vs endpoint — Trial 1 — `recipe-mom-12-0-top5`: the recipe on its full window

**25/25 endpoints beat SPY TR; 0/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +492.56% | +394.58% | +97.98% | 0.077 | 0.600 | FAIL |
| 2024-08-30 | +462.19% | +406.13% | +56.05% | 0.106 | 0.578 | FAIL |
| 2024-09-30 | +512.45% | +416.77% | +95.68% | 0.080 | 0.606 | FAIL |
| 2024-10-31 | +544.15% | +412.16% | +131.99% | 0.063 | 0.623 | FAIL |
| 2024-11-29 | +710.19% | +442.70% | +267.49% | 0.026 | 0.696 | FAIL |
| 2024-12-31 | +641.32% | +429.62% | +211.70% | 0.030 | 0.665 | FAIL |
| 2025-01-31 | +700.55% | +443.84% | +256.71% | 0.023 | 0.686 | FAIL |
| 2025-02-28 | +633.79% | +436.94% | +196.85% | 0.033 | 0.654 | FAIL |
| 2025-03-31 | +565.53% | +407.02% | +158.51% | 0.052 | 0.618 | FAIL |
| 2025-04-30 | +633.06% | +402.62% | +230.44% | 0.024 | 0.642 | FAIL |
| 2025-05-30 | +712.99% | +434.21% | +278.78% | 0.018 | 0.674 | FAIL |
| 2025-06-30 | +759.57% | +461.67% | +297.91% | 0.016 | 0.691 | FAIL |
| 2025-07-31 | +812.83% | +474.60% | +338.23% | 0.010 | 0.707 | FAIL |
| 2025-08-29 | +768.06% | +486.39% | +281.67% | 0.018 | 0.691 | FAIL |
| 2025-09-30 | +809.68% | +507.27% | +302.41% | 0.015 | 0.704 | FAIL |
| 2025-10-31 | +798.35% | +521.75% | +276.60% | 0.018 | 0.699 | FAIL |
| 2025-11-28 | +762.77% | +522.96% | +239.81% | 0.021 | 0.683 | FAIL |
| 2025-12-31 | +738.08% | +523.44% | +214.63% | 0.024 | 0.673 | FAIL |
| 2026-01-30 | +941.70% | +532.63% | +409.07% | 0.010 | 0.732 | FAIL |
| 2026-02-27 | +992.90% | +527.16% | +465.74% | 0.007 | 0.742 | FAIL |
| 2026-03-31 | +962.70% | +496.22% | +466.49% | 0.004 | 0.726 | FAIL |
| 2026-04-30 | +1532.55% | +558.85% | +973.70% | 0.000 | 0.825 | FAIL |
| 2026-05-29 | +1900.91% | +593.52% | +1307.39% | 0.000 | 0.860 | FAIL |
| 2026-06-30 | +2205.05% | +586.37% | +1618.68% | 0.000 | 0.876 | FAIL |
| 2026-07-17 | +1625.01% | +583.17% | +1041.84% | 0.000 | 0.818 | FAIL |

# Pre-committed kill trial (demote-only)

Identical recipe, evaluation start 2016-01-01 — pre-committed in the spec BEFORE any result existed. A PASS here validates nothing by itself; a FAIL is a strike.

## Trial 2 — `recipe-mom-12-0-top5-2016`: the kill window

Family `recipe-mom-12-0-top5-2016`; evaluation start 2016-01-04; 126 rebalances; forced delisting liquidations 0; unfilled buys 0.

Return +746.62%, Sharpe 0.73, max drawdown -53.85%, avg turnover 74.53% per rebalance (sum |Δw|, both sides)

### Gate verdict: **FAIL**

- verdict: **FAIL**
- strategy TOTAL return: +746.62%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +357.80%
- margin over SPY TR: +388.83%
- equal-weight all-eligible TR (informational, NOT binding): +283.88%
- null-model p-value: 0.001 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.658 at n_trials=23 (lineage 'momentum', 23 registered trials; must be >= 0.9)
- trial registry id: `db5bf94e-e9e3-4e9a-a317-429115b4c53f` (registered and COMMITTED before the run; metrics enriched on the same row after)

Verbatim gate reasons:
- deflated Sharpe 0.66 < 0.9 at n_trials=23

### Walk-forward: 3/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +70.58% | +55.33% | +15.25% |
| 2 | -4.13% | +47.52% | -51.65% |
| 3 | +47.13% | +12.07% | +35.06% |
| 4 | +247.00% | +68.51% | +178.49% |

- mean return +90.15%, mean Sharpe 0.74, worst fold -4.13%

### Exhibit: verdict vs endpoint — Trial 2 — `recipe-mom-12-0-top5-2016`: the kill window

**19/25 endpoints beat SPY TR; 0/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +190.82% | +231.42% | -40.60% | 0.280 | 0.353 | FAIL |
| 2024-08-30 | +175.92% | +239.16% | -63.24% | 0.352 | 0.331 | FAIL |
| 2024-09-30 | +200.59% | +246.29% | -45.70% | 0.286 | 0.362 | FAIL |
| 2024-10-31 | +216.14% | +243.20% | -27.06% | 0.223 | 0.380 | FAIL |
| 2024-11-29 | +297.64% | +263.67% | +33.97% | 0.095 | 0.467 | FAIL |
| 2024-12-31 | +263.83% | +254.90% | +8.93% | 0.110 | 0.431 | FAIL |
| 2025-01-31 | +292.90% | +264.43% | +28.47% | 0.083 | 0.457 | FAIL |
| 2025-02-28 | +260.14% | +259.81% | +0.34% | 0.129 | 0.420 | FAIL |
| 2025-03-31 | +226.64% | +239.76% | -13.12% | 0.176 | 0.381 | FAIL |
| 2025-04-30 | +259.78% | +236.81% | +22.97% | 0.105 | 0.412 | FAIL |
| 2025-05-30 | +299.01% | +257.98% | +41.03% | 0.062 | 0.449 | FAIL |
| 2025-06-30 | +321.87% | +276.38% | +45.50% | 0.058 | 0.469 | FAIL |
| 2025-07-31 | +348.01% | +285.04% | +62.97% | 0.041 | 0.490 | FAIL |
| 2025-08-29 | +326.04% | +292.95% | +33.09% | 0.070 | 0.470 | FAIL |
| 2025-09-30 | +346.47% | +306.94% | +39.53% | 0.052 | 0.487 | FAIL |
| 2025-10-31 | +340.90% | +316.64% | +24.27% | 0.052 | 0.481 | FAIL |
| 2025-11-28 | +323.44% | +317.45% | +5.99% | 0.076 | 0.464 | FAIL |
| 2025-12-31 | +311.32% | +317.77% | -6.45% | 0.096 | 0.452 | FAIL |
| 2026-01-30 | +411.26% | +323.93% | +87.33% | 0.041 | 0.526 | FAIL |
| 2026-02-27 | +436.39% | +320.27% | +116.12% | 0.037 | 0.541 | FAIL |
| 2026-03-31 | +421.57% | +299.53% | +122.04% | 0.028 | 0.523 | FAIL |
| 2026-04-30 | +701.25% | +341.50% | +359.75% | 0.001 | 0.663 | FAIL |
| 2026-05-29 | +882.04% | +364.73% | +517.30% | 0.001 | 0.719 | FAIL |
| 2026-06-30 | +1031.30% | +359.94% | +671.36% | 0.000 | 0.747 | FAIL |
| 2026-07-17 | +746.62% | +357.80% | +388.83% | 0.001 | 0.658 | FAIL |

## Summary

| trial | window | strategy TR | SPY TR | margin | null p | DSR (n) | WF+ | endpoints beat/pass/total | verdict |
|---|---|---|---|---|---|---|---|---|---|
| `recipe-mom-12-0-top5` | 2012-07-02 → 2026-07-17 | +1625.01% | +583.17% | +1041.84% | 0.000 | 0.818 (22) | 4/4 | 25/0/25 | **FAIL** |
| `recipe-mom-12-0-top5-2016` | 2016-01-04 → 2026-07-17 | +746.62% | +357.80% | +388.83% | 0.001 | 0.658 (23) | 3/4 | 19/0/25 | **FAIL** |

Trial registry: **47 trials before this run → 49 after** (two pre-committed registrations; lineage 'momentum' count now 23).

## Approval status

**None sought here — by design.** A recipe PASS only means the recipe may be taken to the separate approval workflow (dcp/backtest/approval.py) by the Principal; the gates were not modified and no strategy row is touched.
