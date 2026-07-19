# RECIPE GAUNTLET — `recipe-mom-3-1-top5` (momentum_3_1, top-5, monthly, pit-sp500)

> ## WHAT THIS IS
> A spec-driven run of the committed portfolio gauntlet (Research Factory v1).
> Everything is imported from the validated runners — engine, eligibility, null
> model, thresholds, walk-forward, delisting rule — and the ranking values come
> from the point-in-time FEATURE STORE, whose equivalence to the production
> signal math is golden-pinned. Verdicts land verbatim, pass or fail.

## The spec (frozen; registered verbatim with both trials)

- spec_hash: `a83034a1cf09b5ab034a7d4bf7e6ff3b34834fafadfe14d97e3b3622baa1774d`
- name: `mom-3-1-top5`; rank_feature: `momentum_3_1`; direction: desc; top_n: 5; rebalance: monthly; universe: pit-sp500
- costs: 10 bps/side, FIXED (the committed CostModel — never a free parameter)
- lineage: `momentum` (ADR-0016 — the deflation count basis)
- rationale (registered as the trial hypothesis, pre-run): Short-horizon continuation: a 3-month formation captures the freshest underreaction (earnings-drift adjacent, Chan-Jegadeesh-Lakonishok 1996) at the cost of a noisier, faster-decaying signal. Hypothesis: the short-horizon momentum premium on the point-in-time S&P 500 panel is strong enough to survive real costs at a monthly rebalance cadence; the literature suggests it is weaker than 12-1, so a FAIL here is informative about horizon, not a surprise. Registered before the result exists.
- pre-committed kill start: 2016-01-01 (demote-only)
- dataset_version: `fc10e98eec7727b516e9ce641824849e2b0b9d8fc6682a8699d4467a01ec9e2c` (the feature store's input-vintage pin)
- ranking basis: stored feature values: split-adjusted PRICE closes — the production signal generator's basis (features/momentum.py, equivalence-pinned); accounting and benchmark are total-return per ADR-0009
- return convention: total return (split-adjusted; each dividend reinvested at its ex-date close — market_data/total_return.py)

## Panel and coverage (loader inherited unchanged)

- Panel 2010-01-04 → 2026-07-17; members with usable series: 657 (70 delisted); missing series: 3; SPY carries 66 reinvested distributions (asserted non-zero)
- Feature materialization: 198 rebalance sessions, 110176 values inserted, 0 already present, 0 failures (fail-loud)
- Null model: 1000-path seeded monkey MC (ADR-0002 #2); walk-forward k=4, horizon=40, embargo=10 (real_run constants, ADR-0002 #3)

## Trial 1 — `recipe-mom-3-1-top5`: the recipe on its full window

Family `recipe-mom-3-1-top5`; evaluation start 2012-07-02; 168 rebalances; forced delisting liquidations 2; unfilled buys 0.

Return +713.33%, Sharpe 0.63, max drawdown -49.21%, avg turnover 156.65% per rebalance (sum |Δw|, both sides)

### Gate verdict: **FAIL**

- verdict: **FAIL**
- strategy TOTAL return: +713.33%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +583.17%
- margin over SPY TR: +130.16%
- equal-weight all-eligible TR (informational, NOT binding): +511.27%
- null-model p-value: 0.055 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.677 at n_trials=20 (lineage 'momentum', 20 registered trials; must be >= 0.9)
- trial registry id: `88f77f04-070a-45af-b884-7ecb5fcbd7a3` (registered and COMMITTED before the run; metrics enriched on the same row after)

Verbatim gate reasons:
- null-model: p=0.055 > 0.05 (random same-universe portfolios do as well)
- deflated Sharpe 0.68 < 0.9 at n_trials=20

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +177.84% | +55.66% | +122.19% |
| 2 | +48.42% | +64.96% | -16.54% |
| 3 | +2.22% | +34.94% | -32.72% |
| 4 | +105.74% | +91.66% | +14.08% |

- mean return +83.55%, mean Sharpe 0.76, worst fold +2.22%

### Exhibit: verdict vs endpoint — Trial 1 — `recipe-mom-3-1-top5`: the recipe on its full window

**12/25 endpoints beat SPY TR; 0/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +286.73% | +394.58% | -107.84% | 0.378 | 0.475 | FAIL |
| 2024-08-30 | +294.12% | +406.13% | -112.01% | 0.380 | 0.480 | FAIL |
| 2024-09-30 | +303.37% | +416.77% | -113.39% | 0.379 | 0.489 | FAIL |
| 2024-10-31 | +270.39% | +412.16% | -141.77% | 0.451 | 0.455 | FAIL |
| 2024-11-29 | +391.88% | +442.70% | -50.82% | 0.242 | 0.560 | FAIL |
| 2024-12-31 | +381.81% | +429.62% | -47.81% | 0.211 | 0.551 | FAIL |
| 2025-01-31 | +431.88% | +443.84% | -11.96% | 0.162 | 0.587 | FAIL |
| 2025-02-28 | +411.95% | +436.94% | -24.99% | 0.185 | 0.570 | FAIL |
| 2025-03-31 | +358.86% | +407.02% | -48.16% | 0.226 | 0.527 | FAIL |
| 2025-04-30 | +357.82% | +402.62% | -44.80% | 0.220 | 0.524 | FAIL |
| 2025-05-30 | +393.82% | +434.21% | -40.39% | 0.192 | 0.551 | FAIL |
| 2025-06-30 | +429.35% | +461.67% | -32.31% | 0.166 | 0.576 | FAIL |
| 2025-07-31 | +487.51% | +474.60% | +12.90% | 0.117 | 0.613 | FAIL |
| 2025-08-29 | +474.93% | +486.39% | -11.46% | 0.137 | 0.603 | FAIL |
| 2025-09-30 | +558.02% | +507.27% | +50.75% | 0.091 | 0.648 | FAIL |
| 2025-10-31 | +580.07% | +521.75% | +58.32% | 0.076 | 0.658 | FAIL |
| 2025-11-28 | +594.09% | +522.96% | +71.12% | 0.078 | 0.659 | FAIL |
| 2025-12-31 | +641.38% | +523.44% | +117.94% | 0.060 | 0.679 | FAIL |
| 2026-01-30 | +772.16% | +532.63% | +239.53% | 0.025 | 0.728 | FAIL |
| 2026-02-27 | +801.46% | +527.16% | +274.30% | 0.028 | 0.737 | FAIL |
| 2026-03-31 | +756.58% | +496.22% | +260.36% | 0.026 | 0.714 | FAIL |
| 2026-04-30 | +942.12% | +558.85% | +383.27% | 0.010 | 0.766 | FAIL |
| 2026-05-29 | +863.80% | +593.52% | +270.27% | 0.017 | 0.743 | FAIL |
| 2026-06-30 | +980.51% | +586.37% | +394.14% | 0.012 | 0.763 | FAIL |
| 2026-07-17 | +713.33% | +583.17% | +130.16% | 0.055 | 0.677 | FAIL |

# Pre-committed kill trial (demote-only)

Identical recipe, evaluation start 2016-01-01 — pre-committed in the spec BEFORE any result existed. A PASS here validates nothing by itself; a FAIL is a strike.

## Trial 2 — `recipe-mom-3-1-top5-2016`: the kill window

Family `recipe-mom-3-1-top5-2016`; evaluation start 2016-01-04; 126 rebalances; forced delisting liquidations 2; unfilled buys 0.

Return +230.42%, Sharpe 0.50, max drawdown -49.21%, avg turnover 158.82% per rebalance (sum |Δw|, both sides)

### Gate verdict: **FAIL**

- verdict: **FAIL**
- strategy TOTAL return: +230.42%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +357.80%
- margin over SPY TR: -127.37%
- equal-weight all-eligible TR (informational, NOT binding): +283.88%
- null-model p-value: 0.338 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.387 at n_trials=21 (lineage 'momentum', 21 registered trials; must be >= 0.9)
- trial registry id: `f97f95d1-5a73-4de4-a40c-9c9532afa8d5` (registered and COMMITTED before the run; metrics enriched on the same row after)

Verbatim gate reasons:
- null-model: p=0.338 > 0.05 (random same-universe portfolios do as well)
- does not beat SPY buy-and-hold (230.4% <= 357.8%)
- deflated Sharpe 0.39 < 0.9 at n_trials=21

### Walk-forward: 3/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +55.13% | +55.33% | -0.20% |
| 2 | +11.40% | +47.52% | -36.12% |
| 3 | -7.66% | +12.07% | -19.73% |
| 4 | +92.19% | +68.51% | +23.67% |

- mean return +37.76%, mean Sharpe 0.51, worst fold -7.66%

### Exhibit: verdict vs endpoint — Trial 2 — `recipe-mom-3-1-top5-2016`: the kill window

**0/25 endpoints beat SPY TR; 0/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +57.11% | +231.42% | -174.31% | 0.887 | 0.167 | FAIL |
| 2024-08-30 | +60.11% | +239.16% | -179.05% | 0.887 | 0.172 | FAIL |
| 2024-09-30 | +63.87% | +246.29% | -182.41% | 0.883 | 0.178 | FAIL |
| 2024-10-31 | +50.47% | +243.20% | -192.72% | 0.919 | 0.156 | FAIL |
| 2024-11-29 | +99.83% | +263.67% | -163.83% | 0.768 | 0.237 | FAIL |
| 2024-12-31 | +95.74% | +254.90% | -159.16% | 0.734 | 0.230 | FAIL |
| 2025-01-31 | +116.08% | +264.43% | -148.35% | 0.654 | 0.262 | FAIL |
| 2025-02-28 | +107.99% | +259.81% | -151.82% | 0.685 | 0.249 | FAIL |
| 2025-03-31 | +86.42% | +239.76% | -153.34% | 0.762 | 0.214 | FAIL |
| 2025-04-30 | +85.99% | +236.81% | -150.82% | 0.732 | 0.213 | FAIL |
| 2025-05-30 | +100.62% | +257.98% | -157.36% | 0.683 | 0.236 | FAIL |
| 2025-06-30 | +115.05% | +276.38% | -161.32% | 0.653 | 0.258 | FAIL |
| 2025-07-31 | +138.68% | +285.04% | -146.37% | 0.560 | 0.293 | FAIL |
| 2025-08-29 | +133.57% | +292.95% | -159.38% | 0.607 | 0.285 | FAIL |
| 2025-09-30 | +167.33% | +306.94% | -139.61% | 0.467 | 0.331 | FAIL |
| 2025-10-31 | +176.28% | +316.64% | -140.35% | 0.413 | 0.342 | FAIL |
| 2025-11-28 | +181.98% | +317.45% | -135.47% | 0.414 | 0.347 | FAIL |
| 2025-12-31 | +201.19% | +317.77% | -116.58% | 0.331 | 0.370 | FAIL |
| 2026-01-30 | +254.32% | +323.93% | -69.61% | 0.210 | 0.430 | FAIL |
| 2026-02-27 | +266.23% | +320.27% | -54.04% | 0.212 | 0.442 | FAIL |
| 2026-03-31 | +247.99% | +299.53% | -51.54% | 0.202 | 0.417 | FAIL |
| 2026-04-30 | +323.37% | +341.50% | -18.13% | 0.113 | 0.488 | FAIL |
| 2026-05-29 | +291.55% | +364.73% | -73.18% | 0.177 | 0.457 | FAIL |
| 2026-06-30 | +338.97% | +359.94% | -20.97% | 0.124 | 0.491 | FAIL |
| 2026-07-17 | +230.42% | +357.80% | -127.37% | 0.338 | 0.387 | FAIL |

## Summary

| trial | window | strategy TR | SPY TR | margin | null p | DSR (n) | WF+ | endpoints beat/pass/total | verdict |
|---|---|---|---|---|---|---|---|---|---|
| `recipe-mom-3-1-top5` | 2012-07-02 → 2026-07-17 | +713.33% | +583.17% | +130.16% | 0.055 | 0.677 (20) | 4/4 | 12/0/25 | **FAIL** |
| `recipe-mom-3-1-top5-2016` | 2016-01-04 → 2026-07-17 | +230.42% | +357.80% | -127.37% | 0.338 | 0.387 (21) | 3/4 | 0/0/25 | **FAIL** |

Trial registry: **45 trials before this run → 47 after** (two pre-committed registrations; lineage 'momentum' count now 21).

## Approval status

**None sought here — by design.** A recipe PASS only means the recipe may be taken to the separate approval workflow (dcp/backtest/approval.py) by the Principal; the gates were not modified and no strategy row is touched.
