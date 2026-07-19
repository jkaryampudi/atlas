# RECIPE GAUNTLET — `recipe-mom-6-1-top5` (momentum_6_1, top-5, monthly, pit-sp500)

> ## WHAT THIS IS
> A spec-driven run of the committed portfolio gauntlet (Research Factory v1).
> Everything is imported from the validated runners — engine, eligibility, null
> model, thresholds, walk-forward, delisting rule — and the ranking values come
> from the point-in-time FEATURE STORE, whose equivalence to the production
> signal math is golden-pinned. Verdicts land verbatim, pass or fail.

## The spec (frozen; registered verbatim with both trials)

- spec_hash: `99d193f1996338399f03d93bee0e9d5eab58917d690d3aa650f61a55e709c36d`
- name: `mom-6-1-top5`; rank_feature: `momentum_6_1`; direction: desc; top_n: 5; rebalance: monthly; universe: pit-sp500
- costs: 10 bps/side, FIXED (the committed CostModel — never a free parameter)
- lineage: `momentum` (ADR-0016 — the deflation count basis)
- rationale (registered as the trial hypothesis, pre-run): Intermediate-horizon continuation (Jegadeesh-Titman 1993): 6-month formation portfolios earn a momentum premium driven by investor underreaction, with a fresher signal than the canonical 12-month formation and less exposure to stale year-old information. Hypothesis: on the point-in-time S&P 500 panel, ranking on 6-1 momentum at top-5/monthly retains a premium that survives costs, the null model, lineage-deflated Sharpe and walk-forward. Registered before the result exists.
- pre-committed kill start: 2016-01-01 (demote-only)
- dataset_version: `b7b6ac8e4e8150ee8127d4239f2f7fc68cc9d23d14e91d4d0319c39ca656864e` (the feature store's input-vintage pin)
- ranking basis: stored feature values: split-adjusted PRICE closes — the production signal generator's basis (features/momentum.py, equivalence-pinned); accounting and benchmark are total-return per ADR-0009
- return convention: total return (split-adjusted; each dividend reinvested at its ex-date close — market_data/total_return.py)

## Panel and coverage (loader inherited unchanged)

- Panel 2010-01-04 → 2026-07-17; members with usable series: 657 (70 delisted); missing series: 3; SPY carries 66 reinvested distributions (asserted non-zero)
- Feature materialization: 198 rebalance sessions, 108217 values inserted, 0 already present, 0 failures (fail-loud)
- Null model: 1000-path seeded monkey MC (ADR-0002 #2); walk-forward k=4, horizon=40, embargo=10 (real_run constants, ADR-0002 #3)

## Trial 1 — `recipe-mom-6-1-top5`: the recipe on its full window

Family `recipe-mom-6-1-top5`; evaluation start 2012-07-02; 168 rebalances; forced delisting liquidations 3; unfilled buys 0.

Return +2616.32%, Sharpe 0.87, max drawdown -41.95%, avg turnover 111.07% per rebalance (sum |Δw|, both sides)

### Gate verdict: **PASS**

- verdict: **PASS**
- strategy TOTAL return: +2616.32%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +583.17%
- margin over SPY TR: +2033.15%
- equal-weight all-eligible TR (informational, NOT binding): +511.27%
- null-model p-value: 0.000 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.921 at n_trials=18 (lineage 'momentum', 18 registered trials; must be >= 0.9)
- trial registry id: `04e3a56d-feb8-48c8-bf33-4fdef50c646c` (registered and COMMITTED before the run; metrics enriched on the same row after)

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +172.07% | +55.66% | +116.42% |
| 2 | +110.83% | +64.96% | +45.87% |
| 3 | +40.30% | +34.94% | +5.36% |
| 4 | +218.36% | +91.66% | +126.70% |

- mean return +135.39%, mean Sharpe 0.96, worst fold +40.30%

### Exhibit: verdict vs endpoint — Trial 1 — `recipe-mom-6-1-top5`: the recipe on its full window

**25/25 endpoints beat SPY TR; 4/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +872.78% | +394.58% | +478.21% | 0.003 | 0.807 | FAIL |
| 2024-08-30 | +806.80% | +406.13% | +400.67% | 0.006 | 0.783 | FAIL |
| 2024-09-30 | +884.55% | +416.77% | +467.78% | 0.003 | 0.804 | FAIL |
| 2024-10-31 | +843.49% | +412.16% | +431.33% | 0.005 | 0.792 | FAIL |
| 2024-11-29 | +1052.27% | +442.70% | +609.57% | 0.000 | 0.840 | FAIL |
| 2024-12-31 | +921.72% | +429.62% | +492.10% | 0.000 | 0.809 | FAIL |
| 2025-01-31 | +1055.88% | +443.84% | +612.03% | 0.000 | 0.835 | FAIL |
| 2025-02-28 | +886.72% | +436.94% | +449.78% | 0.004 | 0.791 | FAIL |
| 2025-03-31 | +751.43% | +407.02% | +344.41% | 0.011 | 0.744 | FAIL |
| 2025-04-30 | +835.91% | +402.62% | +433.29% | 0.001 | 0.760 | FAIL |
| 2025-05-30 | +920.44% | +434.21% | +486.23% | 0.000 | 0.783 | FAIL |
| 2025-06-30 | +985.72% | +461.67% | +524.05% | 0.000 | 0.798 | FAIL |
| 2025-07-31 | +1000.80% | +474.60% | +526.20% | 0.000 | 0.801 | FAIL |
| 2025-08-29 | +932.67% | +486.39% | +446.28% | 0.004 | 0.784 | FAIL |
| 2025-09-30 | +1097.99% | +507.27% | +590.71% | 0.000 | 0.819 | FAIL |
| 2025-10-31 | +1203.21% | +521.75% | +681.46% | 0.000 | 0.835 | FAIL |
| 2025-11-28 | +1242.64% | +522.96% | +719.68% | 0.000 | 0.838 | FAIL |
| 2025-12-31 | +1321.31% | +523.44% | +797.87% | 0.000 | 0.848 | FAIL |
| 2026-01-30 | +1701.75% | +532.63% | +1169.12% | 0.000 | 0.888 | FAIL |
| 2026-02-27 | +1765.52% | +527.16% | +1238.36% | 0.000 | 0.893 | FAIL |
| 2026-03-31 | +1703.31% | +496.22% | +1207.09% | 0.000 | 0.880 | FAIL |
| 2026-04-30 | +2499.87% | +558.85% | +1941.02% | 0.000 | 0.928 | PASS |
| 2026-05-29 | +3004.20% | +593.52% | +2410.67% | 0.000 | 0.944 | PASS |
| 2026-06-30 | +3537.55% | +586.37% | +2951.18% | 0.000 | 0.953 | PASS |
| 2026-07-17 | +2616.32% | +583.17% | +2033.15% | 0.000 | 0.921 | PASS |

# Pre-committed kill trial (demote-only)

Identical recipe, evaluation start 2016-01-01 — pre-committed in the spec BEFORE any result existed. A PASS here validates nothing by itself; a FAIL is a strike.

## Trial 2 — `recipe-mom-6-1-top5-2016`: the kill window

Family `recipe-mom-6-1-top5-2016`; evaluation start 2016-01-04; 126 rebalances; forced delisting liquidations 3; unfilled buys 0.

Return +976.12%, Sharpe 0.80, max drawdown -41.95%, avg turnover 112.23% per rebalance (sum |Δw|, both sides)

### Gate verdict: **FAIL**

- verdict: **FAIL**
- strategy TOTAL return: +976.12%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +357.80%
- margin over SPY TR: +618.32%
- equal-weight all-eligible TR (informational, NOT binding): +283.88%
- null-model p-value: 0.001 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.767 at n_trials=19 (lineage 'momentum', 19 registered trials; must be >= 0.9)
- trial registry id: `c40949d7-40c9-4d57-86f8-f56d3f429c7e` (registered and COMMITTED before the run; metrics enriched on the same row after)

Verbatim gate reasons:
- deflated Sharpe 0.77 < 0.9 at n_trials=19

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +110.72% | +55.33% | +55.39% |
| 2 | +46.60% | +47.52% | -0.93% |
| 3 | +34.45% | +12.07% | +22.38% |
| 4 | +153.86% | +68.51% | +85.35% |

- mean return +86.41%, mean Sharpe 0.86, worst fold +34.45%

### Exhibit: verdict vs endpoint — Trial 2 — `recipe-mom-6-1-top5-2016`: the kill window

**24/25 endpoints beat SPY TR; 0/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +285.39% | +231.42% | +53.97% | 0.072 | 0.511 | FAIL |
| 2024-08-30 | +259.25% | +239.16% | +20.08% | 0.122 | 0.477 | FAIL |
| 2024-09-30 | +290.05% | +246.29% | +43.76% | 0.085 | 0.510 | FAIL |
| 2024-10-31 | +273.78% | +243.20% | +30.58% | 0.098 | 0.492 | FAIL |
| 2024-11-29 | +356.49% | +263.67% | +92.83% | 0.039 | 0.572 | FAIL |
| 2024-12-31 | +304.77% | +254.90% | +49.87% | 0.058 | 0.520 | FAIL |
| 2025-01-31 | +357.92% | +264.43% | +93.49% | 0.034 | 0.567 | FAIL |
| 2025-02-28 | +290.91% | +259.81% | +31.10% | 0.088 | 0.499 | FAIL |
| 2025-03-31 | +237.31% | +239.76% | -2.45% | 0.148 | 0.435 | FAIL |
| 2025-04-30 | +270.78% | +236.81% | +33.97% | 0.084 | 0.463 | FAIL |
| 2025-05-30 | +304.26% | +257.98% | +46.29% | 0.059 | 0.496 | FAIL |
| 2025-06-30 | +330.13% | +276.38% | +53.75% | 0.046 | 0.520 | FAIL |
| 2025-07-31 | +336.10% | +285.04% | +51.06% | 0.049 | 0.524 | FAIL |
| 2025-08-29 | +309.11% | +292.95% | +16.17% | 0.093 | 0.499 | FAIL |
| 2025-09-30 | +374.60% | +306.94% | +67.67% | 0.035 | 0.554 | FAIL |
| 2025-10-31 | +416.29% | +316.64% | +99.65% | 0.023 | 0.583 | FAIL |
| 2025-11-28 | +431.91% | +317.45% | +114.46% | 0.022 | 0.590 | FAIL |
| 2025-12-31 | +463.08% | +317.77% | +145.31% | 0.011 | 0.608 | FAIL |
| 2026-01-30 | +613.79% | +323.93% | +289.87% | 0.001 | 0.686 | FAIL |
| 2026-02-27 | +639.06% | +320.27% | +318.79% | 0.001 | 0.695 | FAIL |
| 2026-03-31 | +614.41% | +299.53% | +314.88% | 0.001 | 0.674 | FAIL |
| 2026-04-30 | +929.99% | +341.50% | +588.49% | 0.000 | 0.777 | FAIL |
| 2026-05-29 | +1129.78% | +364.73% | +765.05% | 0.000 | 0.816 | FAIL |
| 2026-06-30 | +1341.08% | +359.94% | +981.14% | 0.000 | 0.842 | FAIL |
| 2026-07-17 | +976.12% | +357.80% | +618.32% | 0.001 | 0.767 | FAIL |

## Summary

| trial | window | strategy TR | SPY TR | margin | null p | DSR (n) | WF+ | endpoints beat/pass/total | verdict |
|---|---|---|---|---|---|---|---|---|---|
| `recipe-mom-6-1-top5` | 2012-07-02 → 2026-07-17 | +2616.32% | +583.17% | +2033.15% | 0.000 | 0.921 (18) | 4/4 | 25/4/25 | **PASS** |
| `recipe-mom-6-1-top5-2016` | 2016-01-04 → 2026-07-17 | +976.12% | +357.80% | +618.32% | 0.001 | 0.767 (19) | 4/4 | 24/0/25 | **FAIL** |

Trial registry: **43 trials before this run → 45 after** (two pre-committed registrations; lineage 'momentum' count now 19).

## Approval status

**None sought here — by design.** A recipe PASS only means the recipe may be taken to the separate approval workflow (dcp/backtest/approval.py) by the Principal; the gates were not modified and no strategy row is touched.
