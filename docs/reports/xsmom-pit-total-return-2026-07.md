# TOTAL-RETURN RE-SCORE — xsmom recipe on the point-in-time S&P 500, scored against SPY TOTAL RETURN (2026-07)

> ## WHY THIS TEST EXISTS
> The board's seven-persona review (docs/reports/board-memo-2026-07.md) found that the
> prior PASS (docs/reports/xsmom-pit-sp500-2026-07.md) was scored against the WRONG BENCHMARK per ADR-0009's
> own text: the ADR requires beating **SPY total return**, and the verdict was scored
> price-return vs price-return because dividends were not ingested anywhere in the
> system. SPY's ~1.9%/yr yield compounds to roughly the size of the entire prior pass
> margin, and the strategy's low-yield momentum tilt makes the correction asymmetric.
> The prior PASS is SUSPENDED and this report re-scores the identical recipe with
> dividends ingested and everything — strategy holdings, monkey null, equal-weight
> benchmark and SPY — on ONE total-return panel. Verdicts land verbatim either way;
> this is a test of the PASS, not a defence of it.

> ## SUPERSESSION
> **The prior PASS (docs/reports/xsmom-pit-sp500-2026-07.md, family `xsmom-pit`) is superseded by this
> report — whatever the verdicts below say.**

## Method

Identical PIT recipe, membership rule, delisting rule, engine, costs, eligibility,
walk-forward constants and gate thresholds as the prior run (all imported, nothing
restated — see that report). TWO pre-committed trials, each registered once:

1. **`xsmom-pit-tr`** — the identical evaluation window (2012-07-02 → 2026-07-10), scored TR-vs-TR.
2. **`xsmom-pit-tr-2016`** — the board's KILL-ONLY subperiod test: identical recipe, evaluation start 2016-01-04 (memo item 2: the 2012-2015 window rides a biased membership undercount and 2016-2025 price-return LOSES to SPY by 14.3pp). It can only demote — a PASS here validates nothing by itself.

TOTAL-RETURN CONVENTION (market_data/total_return.py, stated once, applied identically
to every series in the panel): each cash dividend is reinvested at its EX-DATE'S CLOSE
— opens and closes share one cumulative factor, so intraday moves are untouched and the
overnight ex-date gap (where the price drops by the detached dividend) absorbs the
compensation. Dividends are stored RAW (market.corporate_actions, action_type='dividend')
and split-adjusted on read, exactly as bars are. A dividend whose ex-date falls after a
delisted series' final bar is dropped (the position was already liquidated to cash) and
counted below.

- Null model: 1000-path monkey MC on the SAME TR panel, identical engine/costs/delisting rule (ADR-0002 #2)
- Gate thresholds IMPORTED from the committed validation module — nothing restated, nothing tuned
- Deflated Sharpe at each family's true registered trial count (ADR-0002 #1)
- Window grade (ADR-0004): `xsmom-pit-tr` 2012-07-02 → 2026-07-10 (>= 10 years — decision-grade); `xsmom-pit-tr-2016` 2016-01-04 → 2026-07-10 (>= 10 years — decision-grade)

## Dividend coverage (honesty section)

- Panel symbols with >= 1 dividend applied: 521; with none stored: 137 (never-payers are normal; the ingest audit event `market.dividends.backfill.completed` separates fetched-none from fetch-failed)
- Dividends reinvested: 27326; dropped before series inception: 540; dropped after a delisted series' final bar: 50; rolled forward to the next session: 11
- SPY (the binding benchmark) carries 66 reinvested distributions — asserted non-zero by the loader

## Re-run 1 — `xsmom-pit-tr`: the identical window, TR-vs-TR

The suspended PASS re-scored honestly. Same evaluation window as the prior report;
the ONLY change is the return convention on both sides of every comparison.

Evaluation start 2012-07-02; family `xsmom-pit-tr`; 168 rebalances; forced delisting liquidations 7; unfilled buys 1.

Return +737.31%, Sharpe 0.82, max drawdown -36.91%, avg turnover 63.05% per rebalance (sum |Δw|, both sides)

### Gate verdict: **PASS**

- verdict: **PASS**
- strategy TOTAL return: +737.31%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +593.89%
- margin over SPY TR: +143.43%
- equal-weight all-eligible TR, monthly (informational, NOT binding): +512.80%
- null-model p-value: 0.000 (must be ≤ 0.05)
- deflated Sharpe: 0.999 at n_trials=1 (must be ≥ 0.9)
- trial registry id: `413a61b6-955d-408c-8bb9-e3760b5fd3ed`

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +86.84% | +57.86% | +28.98% |
| 2 | +48.82% | +66.06% | -17.24% |
| 3 | +41.84% | +36.02% | +5.82% |
| 4 | +124.72% | +94.67% | +30.05% |

- mean return +75.56%, mean Sharpe 0.90, worst fold +41.84%

### Exhibit: verdict vs endpoint — Re-run 1 — `xsmom-pit-tr`: the identical window, TR-vs-TR

The identical run re-judged at the final date and each of the prior 24 month-ends (exact truncation of the stored strategy/SPY/null curves — see verdict_vs_endpoint). A robust edge should not need a particular month to end on.

**8/25 endpoints beat SPY TR; 8/25 endpoints PASS the full gate.**

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +363.08% | +394.58% | -31.50% | 0.027 | 0.994 | FAIL |
| 2024-08-30 | +381.62% | +406.13% | -24.52% | 0.015 | 0.995 | FAIL |
| 2024-09-30 | +397.49% | +416.77% | -19.28% | 0.012 | 0.995 | FAIL |
| 2024-10-31 | +394.07% | +412.16% | -18.09% | 0.009 | 0.995 | FAIL |
| 2024-11-29 | +440.63% | +442.70% | -2.06% | 0.004 | 0.997 | FAIL |
| 2024-12-31 | +414.38% | +429.62% | -15.23% | 0.002 | 0.996 | FAIL |
| 2025-01-31 | +451.62% | +443.84% | +7.78% | 0.001 | 0.997 | PASS |
| 2025-02-28 | +431.86% | +436.94% | -5.08% | 0.001 | 0.996 | FAIL |
| 2025-03-31 | +388.53% | +407.02% | -18.49% | 0.010 | 0.994 | FAIL |
| 2025-04-30 | +395.26% | +402.62% | -7.37% | 0.004 | 0.994 | FAIL |
| 2025-05-30 | +423.53% | +434.21% | -10.69% | 0.002 | 0.995 | FAIL |
| 2025-06-30 | +443.57% | +461.67% | -18.09% | 0.001 | 0.996 | FAIL |
| 2025-07-31 | +444.84% | +474.60% | -29.76% | 0.001 | 0.996 | FAIL |
| 2025-08-29 | +448.08% | +486.39% | -38.31% | 0.004 | 0.996 | FAIL |
| 2025-09-30 | +493.53% | +507.27% | -13.74% | 0.001 | 0.997 | FAIL |
| 2025-10-31 | +497.15% | +521.75% | -24.60% | 0.001 | 0.997 | FAIL |
| 2025-11-28 | +488.14% | +522.96% | -34.83% | 0.001 | 0.997 | FAIL |
| 2025-12-31 | +501.02% | +523.44% | -22.42% | 0.001 | 0.997 | FAIL |
| 2026-01-30 | +547.34% | +532.63% | +14.71% | 0.001 | 0.998 | PASS |
| 2026-02-27 | +569.33% | +527.16% | +42.16% | 0.000 | 0.998 | PASS |
| 2026-03-31 | +523.17% | +496.22% | +26.95% | 0.000 | 0.997 | PASS |
| 2026-04-30 | +669.97% | +558.85% | +111.11% | 0.000 | 0.999 | PASS |
| 2026-05-29 | +732.35% | +593.52% | +138.83% | 0.000 | 0.999 | PASS |
| 2026-06-30 | +793.09% | +586.37% | +206.72% | 0.000 | 0.999 | PASS |
| 2026-07-10 | +737.31% | +593.89% | +143.43% | 0.000 | 0.999 | PASS |

## Re-run 2 — `xsmom-pit-tr-2016`: the board's kill test (start 2016-01-04)

KILL-ONLY (pre-committed): removes the biased early-membership window and the
2012-2015 head start; a FAIL here demotes the strategy regardless of Re-run 1.

Evaluation start 2016-01-04; family `xsmom-pit-tr-2016`; 126 rebalances; forced delisting liquidations 7; unfilled buys 1.

Return +377.32%, Sharpe 0.77, max drawdown -36.91%, avg turnover 63.93% per rebalance (sum |Δw|, both sides)

### Gate verdict: **PASS**

- verdict: **PASS**
- strategy TOTAL return: +377.32%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +364.98%
- margin over SPY TR: +12.34%
- equal-weight all-eligible TR, monthly (informational, NOT binding): +284.84%
- null-model p-value: 0.000 (must be ≤ 0.05)
- deflated Sharpe: 0.994 at n_trials=1 (must be ≥ 0.9)
- trial registry id: `da38aa8e-3696-4eab-9920-36fb39888169`

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +43.70% | +54.79% | -11.09% |
| 2 | +26.40% | +47.44% | -21.04% |
| 3 | +9.95% | +11.32% | -1.36% |
| 4 | +117.82% | +71.16% | +46.66% |

- mean return +49.47%, mean Sharpe 0.78, worst fold +9.95%

### Exhibit: verdict vs endpoint — Re-run 2 — `xsmom-pit-tr-2016`: the board's kill test (start 2016-01-04)

The identical run re-judged at the final date and each of the prior 24 month-ends (exact truncation of the stored strategy/SPY/null curves — see verdict_vs_endpoint). A robust edge should not need a particular month to end on.

**3/25 endpoints beat SPY TR; 3/25 endpoints PASS the full gate.**

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +163.98% | +231.42% | -67.44% | 0.327 | 0.968 | FAIL |
| 2024-08-30 | +174.55% | +239.16% | -64.61% | 0.271 | 0.971 | FAIL |
| 2024-09-30 | +183.60% | +246.29% | -62.69% | 0.237 | 0.974 | FAIL |
| 2024-10-31 | +181.65% | +243.20% | -61.55% | 0.192 | 0.973 | FAIL |
| 2024-11-29 | +208.19% | +263.67% | -55.47% | 0.119 | 0.981 | FAIL |
| 2024-12-31 | +193.23% | +254.90% | -61.67% | 0.097 | 0.976 | FAIL |
| 2025-01-31 | +214.45% | +264.43% | -49.98% | 0.045 | 0.981 | FAIL |
| 2025-02-28 | +203.19% | +259.81% | -56.61% | 0.087 | 0.978 | FAIL |
| 2025-03-31 | +178.49% | +239.76% | -61.26% | 0.194 | 0.970 | FAIL |
| 2025-04-30 | +182.33% | +236.81% | -54.49% | 0.105 | 0.969 | FAIL |
| 2025-05-30 | +198.44% | +257.98% | -59.54% | 0.081 | 0.974 | FAIL |
| 2025-06-30 | +209.87% | +276.38% | -66.51% | 0.072 | 0.977 | FAIL |
| 2025-07-31 | +210.59% | +285.04% | -74.45% | 0.092 | 0.978 | FAIL |
| 2025-08-29 | +212.44% | +292.95% | -80.51% | 0.126 | 0.978 | FAIL |
| 2025-09-30 | +238.35% | +306.94% | -68.59% | 0.041 | 0.983 | FAIL |
| 2025-10-31 | +240.41% | +316.64% | -76.23% | 0.025 | 0.983 | FAIL |
| 2025-11-28 | +235.27% | +317.45% | -82.18% | 0.050 | 0.982 | FAIL |
| 2025-12-31 | +242.62% | +317.77% | -75.16% | 0.036 | 0.983 | FAIL |
| 2026-01-30 | +269.02% | +323.93% | -54.91% | 0.015 | 0.987 | FAIL |
| 2026-02-27 | +281.55% | +320.27% | -38.71% | 0.012 | 0.988 | FAIL |
| 2026-03-31 | +255.24% | +299.53% | -44.29% | 0.017 | 0.984 | FAIL |
| 2026-04-30 | +338.92% | +341.50% | -2.58% | 0.001 | 0.993 | FAIL |
| 2026-05-29 | +374.49% | +364.73% | +9.75% | 0.000 | 0.994 | PASS |
| 2026-06-30 | +409.11% | +359.94% | +49.17% | 0.000 | 0.995 | PASS |
| 2026-07-10 | +377.32% | +364.98% | +12.34% | 0.000 | 0.994 | PASS |

### Exhibit: per-calendar-year total returns

Identical engine, panel and costs in every column; the 2016-start column is the kill-test run (all-cash until its first rebalance, so its 2016 is partial by construction). SPY TR column from the full-window benchmark run.

| year | strategy TR (full) | strategy TR (2016 start) | SPY TR | note |
|---|---|---|---|---|
| 2012 | +8.68% | — | +3.86% | partial (from 2012-07-02) |
| 2013 | +41.59% | — | +32.31% |  |
| 2014 | +12.63% | — | +13.46% |  |
| 2015 | +7.81% | — | +1.25% |  |
| 2016 | +3.62% | +10.36% | +12.00% |  |
| 2017 | +17.97% | +17.97% | +21.70% |  |
| 2018 | -6.85% | -6.85% | -4.56% |  |
| 2019 | +25.84% | +25.84% | +31.22% |  |
| 2020 | +16.98% | +16.98% | +18.37% |  |
| 2021 | +18.00% | +18.00% | +28.75% |  |
| 2022 | -0.76% | -0.76% | -18.17% |  |
| 2023 | +10.77% | +10.77% | +26.19% |  |
| 2024 | +26.62% | +26.62% | +24.89% |  |
| 2025 | +16.84% | +16.84% | +17.72% |  |
| 2026 | +39.32% | +39.32% | +11.30% | partial (through 2026-07-10) |

## Summary

| trial | window | strategy TR | SPY TR | margin | null p | DSR (n) | WF+ | verdict |
|---|---|---|---|---|---|---|---|---|
| `xsmom-pit-tr` | 2012-07-02 → 2026-07-10 | +737.31% | +593.89% | +143.43% | 0.000 | 0.999 (1) | 4/4 | **PASS** |
| `xsmom-pit-tr-2016` | 2016-01-04 → 2026-07-10 | +377.32% | +364.98% | +12.34% | 0.000 | 0.994 (1) | 4/4 | **PASS** |

Trial registry: **25 trials before this re-score → 27 after** (one `xsmom-pit-tr` trial, one `xsmom-pit-tr-2016` trial).

## Annual outcome distribution

> **History is not a forecast.** This is the DISPERSION a strategy like this has
> exhibited — any single future year can land anywhere in (or outside) this range;
> the median is not a promise.

Block bootstrap of annual TOTAL-return outcomes: daily returns resampled in 21-session blocks, 1000 seeded draws of 252 sessions (seed 7); paired draws, same method for both columns.

| percentile of simulated annual return | strategy | SPY B&H |
|---|---|---|
| 10th | -6.57% | -4.38% |
| 25th | +3.94% | +5.29% |
| median | +16.50% | +15.68% |
| 75th | +29.95% | +25.93% |
| 90th | +44.94% | +36.94% |

## Verdict disposition

- Re-run 1 (`xsmom-pit-tr`, identical window, TR-vs-TR): **PASS**
- Re-run 2 (`xsmom-pit-tr-2016`, kill-only, start 2016-01-04): **PASS**
- The prior PASS is superseded by this report.

## Approval status

**None sought here — by design.** This is a VALIDATION re-score on a membership-gated universe built from validation-only instruments; it does not itself qualify any strategy for the approval workflow (dcp/backtest/approval.py). The gates were not modified; no strategy row is touched.
