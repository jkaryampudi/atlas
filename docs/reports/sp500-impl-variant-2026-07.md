# TOP-5 OF THE FULL S&P 500 — the expansion's live form through the identical gauntlet (2026-07)

> ## WHY THIS TEST EXISTS (ADR-0016, pending — the missing trial)
> The live momentum sleeve was validated as `xsmom-impl-tr`: top-5 of the TOP-100-by-dollar-volume
> subset of the point-in-time S&P 500 (docs/reports/implementable-variant-2026-07.md). The proposed
> universe expansion (ADR-0016) would make ~500 names active, so the LIVE form becomes top-5 of
> the FULL point-in-time S&P 500 — the extreme tail of a ~5x wider cross-section with NO liquidity
> screen. That is a materially different portfolio and NO existing trial covered it. This run
> registers it — family `xsmom-impl500-tr` plus the pre-committed kill sibling — and the
> verdicts land verbatim, pass or fail. PEAD is not run: its sleeve budget is 0 (ADR-0015).

> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)
> Evaluation window 2012-07-02 → 2026-07-15 (>= 10 years); verdicts are decision-grade
> FOR THE EXPANSION QUESTION — pass or fail, recorded verbatim.

## Universe construction

The point-in-time S&P 500 (validation.index_membership, fail-closed interval rule,
delisted names included — the same membership the decile runs and the impl-variant
run validated on), with **no liquidity screen of any kind**: at each rebalance the
base IS the whole eligible set (member at t + price at t + 252 sessions of
history). The dollar-volume matrix is not even loaded in this mode — no liquidity
data can influence selection, structurally. Consequences stated up front:

- The ranked cross-section is ~5x wider than the validated top-100 base, and the
  top-5 sits in its extreme momentum tail — names the ADV screen existed to exclude
  (thinner, smaller, closer to delisting) are now selectable.
- Forced delisting liquidations this run: 4 (the top-5-of-100 run recorded 5 — cited); unfilled buys: 0.
- India ADRs remain excluded (no US index membership exists for them); the India
  sleeve remains unvalidated by construction.
- The early-window membership undercount that flattered every PIT run flatters this
  one identically.

### Panel and coverage (inherited loaders, unchanged)

- Panel 2010-01-04 → 2026-07-15 (4157 aligned XNYS sessions), total-return convention on every series
  (dividends reinvested at the ex-date close; identical on both sides of every comparison)
- Members with usable series: 657 (70 delisted); missing series: 3; SPY carries 66 reinvested distributions (asserted non-zero)
- Rebalance-set sizes at the final rebalance (2026-06-30): 502 members / 494 eligible / base == eligible (no screen)
- December snapshots (members/eligible = base): 2012: 347/342; 2013: 366/360; 2014: 381/376; 2015: 403/395; 2016: 424/420; 2017: 445/443; 2018: 460/457; 2019: 475/468; 2020: 484/477; 2021: 493/486; 2022: 496/489; 2023: 501/495; 2024: 502/495; 2025: 502/496

## Method (everything imported, nothing restated)

- Construction: top-5 equal weight (SLEEVE_MAX_NAMES — the live cap, imported); monthly rebalance at month-end close, execution next session's open; costs 5+5 bps/side on turnover
- Null model: 1000-path monkey MC, min(5, |eligible|) names drawn uniformly from the SAME full eligible set, identical engine/costs/delisting rule (ADR-0002 #2)
- Walk-forward: purged+embargoed, k=4, horizon=40, embargo=10, warmup = evaluation-start index (ADR-0002 #3)
- Deflated Sharpe at the family's true registered trial count (ADR-0002 #1); every run registered in quant.trial_registry
- Binding benchmark: SPY buy-and-hold TOTAL return over the same window (ADR-0009)
- Pre-committed kill-only trial: evaluation start 2016-01-01 (imported board commitment) — it can only demote, never validate

## Full window — `xsmom-impl500-tr`: momentum 12-1, top-5 of the FULL PIT S&P 500

Family `xsmom-impl500-tr`; evaluation start 2012-07-02; 168 rebalances; forced delisting liquidations 4; unfilled buys 0.

Return +2235.12%, Sharpe 0.82, max drawdown -42.74%, avg turnover 73.44% per rebalance (sum |Δw|, both sides)

### Gate verdict: **PASS**

- verdict: **PASS**
- strategy TOTAL return: +2235.12%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +593.76%
- margin over SPY TR: +1641.36%
- equal-weight whole-eligible-base TR (informational, NOT binding): +511.80%
- null-model p-value: 0.000 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.999 at n_trials=1 (must be >= 0.9)
- trial registry id: `6b4b582a-9e29-474d-bba2-1a0893ea5d45`

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +139.71% | +55.66% | +84.06% |
| 2 | +91.05% | +64.96% | +26.08% |
| 3 | +33.13% | +34.94% | -1.81% |
| 4 | +293.53% | +94.63% | +198.89% |

- mean return +139.35%, mean Sharpe 0.89, worst fold +33.13%

### Exhibit: verdict vs endpoint — Full window — `xsmom-impl500-tr`: momentum 12-1, top-5 of the FULL PIT S&P 500

**25/25 endpoints beat SPY TR; 25/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +609.27% | +394.58% | +214.69% | 0.029 | 0.991 | PASS |
| 2024-08-30 | +597.44% | +406.13% | +191.31% | 0.043 | 0.990 | PASS |
| 2024-09-30 | +647.27% | +416.77% | +230.50% | 0.031 | 0.992 | PASS |
| 2024-10-31 | +677.26% | +412.16% | +265.10% | 0.024 | 0.992 | PASS |
| 2024-11-29 | +846.51% | +442.70% | +403.82% | 0.008 | 0.995 | PASS |
| 2024-12-31 | +756.29% | +429.62% | +326.67% | 0.011 | 0.994 | PASS |
| 2025-01-31 | +835.87% | +443.84% | +392.03% | 0.007 | 0.995 | PASS |
| 2025-02-28 | +762.56% | +436.94% | +325.62% | 0.013 | 0.993 | PASS |
| 2025-03-31 | +682.29% | +407.02% | +275.27% | 0.019 | 0.991 | PASS |
| 2025-04-30 | +783.72% | +402.62% | +381.10% | 0.005 | 0.993 | PASS |
| 2025-05-30 | +885.16% | +434.21% | +450.94% | 0.000 | 0.995 | PASS |
| 2025-06-30 | +925.80% | +461.67% | +464.13% | 0.000 | 0.995 | PASS |
| 2025-07-31 | +996.09% | +474.60% | +521.48% | 0.000 | 0.996 | PASS |
| 2025-08-29 | +950.85% | +486.39% | +464.46% | 0.004 | 0.995 | PASS |
| 2025-09-30 | +1000.52% | +507.27% | +493.24% | 0.001 | 0.996 | PASS |
| 2025-10-31 | +986.81% | +521.75% | +465.06% | 0.002 | 0.996 | PASS |
| 2025-11-28 | +944.36% | +522.96% | +421.39% | 0.005 | 0.995 | PASS |
| 2025-12-31 | +912.80% | +523.44% | +389.35% | 0.009 | 0.994 | PASS |
| 2026-01-30 | +1191.14% | +532.63% | +658.51% | 0.000 | 0.997 | PASS |
| 2026-02-27 | +1258.35% | +527.16% | +731.19% | 0.000 | 0.997 | PASS |
| 2026-03-31 | +1224.42% | +496.22% | +728.21% | 0.000 | 0.997 | PASS |
| 2026-04-30 | +1875.46% | +558.85% | +1316.61% | 0.000 | 0.999 | PASS |
| 2026-05-29 | +2319.13% | +593.52% | +1725.61% | 0.000 | 0.999 | PASS |
| 2026-06-30 | +2559.26% | +586.37% | +1972.89% | 0.000 | 0.999 | PASS |
| 2026-07-15 | +2235.12% | +593.76% | +1641.36% | 0.000 | 0.999 | PASS |

## Pre-committed 2016 kill — `xsmom-impl500-tr-2016`: same recipe, demote-only

Family `xsmom-impl500-tr-2016`; evaluation start 2016-01-04; 126 rebalances; forced delisting liquidations 4; unfilled buys 0.

Return +991.82%, Sharpe 0.80, max drawdown -42.74%, avg turnover 78.55% per rebalance (sum |Δw|, both sides)

### Gate verdict: **PASS**

- verdict: **PASS**
- strategy TOTAL return: +991.82%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +364.89%
- margin over SPY TR: +626.92%
- equal-weight whole-eligible-base TR (informational, NOT binding): +284.21%
- null-model p-value: 0.001 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.995 at n_trials=1 (must be >= 0.9)
- trial registry id: `84aae4ea-985a-48dc-9460-aeeb1460ef7c`

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +70.42% | +54.79% | +15.63% |
| 2 | +10.82% | +47.44% | -36.62% |
| 3 | +49.60% | +11.32% | +38.28% |
| 4 | +270.42% | +71.12% | +199.30% |

- mean return +100.32%, mean Sharpe 0.79, worst fold +10.82%

### Exhibit: verdict vs endpoint — Pre-committed 2016 kill — `xsmom-impl500-tr-2016`: same recipe, demote-only

**24/25 endpoints beat SPY TR; 18/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +231.63% | +231.42% | +0.21% | 0.158 | 0.957 | FAIL |
| 2024-08-30 | +226.10% | +239.16% | -13.07% | 0.186 | 0.955 | FAIL |
| 2024-09-30 | +249.40% | +246.29% | +3.11% | 0.152 | 0.961 | FAIL |
| 2024-10-31 | +263.42% | +243.20% | +20.22% | 0.113 | 0.964 | FAIL |
| 2024-11-29 | +342.56% | +263.67% | +78.89% | 0.049 | 0.976 | PASS |
| 2024-12-31 | +300.37% | +254.90% | +45.47% | 0.064 | 0.970 | FAIL |
| 2025-01-31 | +337.58% | +264.43% | +73.15% | 0.046 | 0.975 | PASS |
| 2025-02-28 | +303.30% | +259.81% | +43.50% | 0.069 | 0.969 | FAIL |
| 2025-03-31 | +265.77% | +239.76% | +26.01% | 0.101 | 0.961 | FAIL |
| 2025-04-30 | +313.20% | +236.81% | +76.39% | 0.041 | 0.969 | PASS |
| 2025-05-30 | +360.62% | +257.98% | +102.64% | 0.027 | 0.975 | PASS |
| 2025-06-30 | +379.63% | +276.38% | +103.25% | 0.025 | 0.977 | PASS |
| 2025-07-31 | +412.49% | +285.04% | +127.45% | 0.013 | 0.980 | PASS |
| 2025-08-29 | +391.34% | +292.95% | +98.39% | 0.027 | 0.978 | PASS |
| 2025-09-30 | +414.56% | +306.94% | +107.63% | 0.023 | 0.980 | PASS |
| 2025-10-31 | +408.15% | +316.64% | +91.52% | 0.026 | 0.979 | PASS |
| 2025-11-28 | +388.30% | +317.45% | +70.85% | 0.038 | 0.977 | PASS |
| 2025-12-31 | +373.55% | +317.77% | +55.78% | 0.044 | 0.975 | PASS |
| 2026-01-30 | +503.69% | +323.93% | +179.76% | 0.007 | 0.985 | PASS |
| 2026-02-27 | +535.12% | +320.27% | +214.85% | 0.008 | 0.986 | PASS |
| 2026-03-31 | +519.25% | +299.53% | +219.72% | 0.006 | 0.985 | PASS |
| 2026-04-30 | +823.65% | +341.50% | +482.15% | 0.000 | 0.994 | PASS |
| 2026-05-29 | +1031.10% | +364.73% | +666.36% | 0.000 | 0.996 | PASS |
| 2026-06-30 | +1143.37% | +359.94% | +783.44% | 0.000 | 0.997 | PASS |
| 2026-07-15 | +991.82% | +364.89% | +626.92% | 0.001 | 0.995 | PASS |

## Exhibit: max drawdown — concentration at the wider tail

The top-5-of-100 validation run drew down -51.97% (cited from docs/reports/implementable-variant-2026-07.md, `xsmom-impl-tr`, same window/seed/paths). The full-universe tail portfolio:

| portfolio | window | max drawdown |
|---|---|---|
| `xsmom-impl500-tr` (top-5 of FULL S&P 500) | 2012-07-02 → 2026-07-15 | **-42.74%** |
| `xsmom-impl500-tr-2016` | 2016-01-04 → 2026-07-15 | **-42.74%** |
| `xsmom-impl-tr` (top-5 of top-100 ADV; cited) | 2012-07-02 → 2026-07-15 | -51.97% |
| SPY buy-and-hold (same window as full run) | 2012-07-02 → 2026-07-15 | -33.70% |

A 5-name book at 20% a name carries single-name gap risk no drawdown statistic
captures; the number above is the historical realisation, not a bound.

### Exhibit: per-calendar-year total returns vs SPY TR

| year | xsmom-impl500-tr | SPY TR |
|---|---|---|
| 2012 | +9.59% | +3.86% |
| 2013 | +42.01% | +32.31% |
| 2014 | +37.24% | +13.46% |
| 2015 | +16.27% | +1.25% |
| 2016 | +4.22% | +12.00% |
| 2017 | +23.49% | +21.70% |
| 2018 | -10.14% | -4.56% |
| 2019 | +44.17% | +31.22% |
| 2020 | +5.65% | +18.37% |
| 2021 | +17.12% | +28.75% |
| 2022 | +9.76% | -18.17% |
| 2023 | +25.10% | +26.19% |
| 2024 | +21.72% | +24.89% |
| 2025 | +18.28% | +17.72% |
| 2026 | +130.56% | +11.28% |

## Summary

| trial | window | strategy TR | SPY TR | margin | null p | DSR (n) | WF+ | endpoints beat/pass | max DD | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| `xsmom-impl500-tr` | 2012-07-02 → 2026-07-15 | +2235.12% | +593.76% | +1641.36% | 0.000 | 0.999 (1) | 4/4 | 25/25/25 | -42.74% | **PASS** |
| `xsmom-impl500-tr-2016` | 2016-01-04 → 2026-07-15 | +991.82% | +364.89% | +626.92% | 0.001 | 0.995 (1) | 4/4 | 24/18/25 | -42.74% | **PASS** |

Trial registry: **41 trials before this run → 43 after** (two trials: the full-window family and its pre-committed kill).

## What this means for the expansion (ADR-0016)

Both trials cleared the binding bar: the portfolio the expansion would actually
trade — top-5 of the full point-in-time S&P 500, no liquidity screen — now has its
own registered evidence, on the same gauntlet the validated top-5-of-100 form
passed. What this run does NOT settle, and ADR-0016 must still answer before
signing:

- **Drawdown**: -42.74% max drawdown on the full window (exhibit above,
  vs -51.97% for top-5-of-100 and -33.70% for SPY). The tolerance bands and DD breakers
  (ADR-0010, DD1-DD3) were tuned against the narrower book; re-derive them before
  the expanded form goes live.
- **Tradability**: 10 bps/side is the validated-universe cost convention. Without a
  liquidity screen the top-5 can land in the thinnest tail of the index; the cost
  model has no evidence there — small-AUM fills likely help, but that is an
  argument, not a measurement.
- **The overlay is still unmodeled**: stops (ADR-0006), L9 staggered entries and L5
  caps are not in this backtest, exactly as they were not in the impl-variant run.

Caveats that survive any verdict: (1) the early-window membership undercount
flatters this run exactly as it flattered every PIT run; (2) endpoint
concentration must be read from the exhibit, not assumed away; (3) costs are the
10 bps/side convention with no liquidity screen behind them; (4) the ADR-0006
stop/entry overlay has no backtest evidence of its own; (5) the India sleeve is
untested by construction.

## Reproduction

Deterministic re-run (official registration against the dev database, after review):

```bash
python -m atlas.dcp.backtest.impl_variant_run --top-universe 0 --paths 1000 --seed 7 --window-end 2026-07-15
```

The `--window-end` pin makes the run byte-identical even after later nightly ingests extend the stored history.

## Approval status

**None sought here — by design.** This is a VALIDATION run on the membership-gated universe; it does not qualify or disqualify any strategy row by itself. Gates were not modified; verdicts are recorded verbatim; whether ADR-0016 is signed is a Principal decision made on this evidence.
