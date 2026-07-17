# THE IMPLEMENTABLE-VARIANT TEST — the live top-5 sleeves on an honest point-in-time large-cap universe (2026-07)

> ## WHY THIS TEST EXISTS (board item 5 — the OPEN obligation of ADR-0010/0013)
> Momentum (`xsmom-pit-tr`) and PEAD (`pead-sue-tr`) were VALIDATED as ~50-name winner
> deciles on the point-in-time S&P 500. The fund TRADES top-5 sleeves on the ~100-name
> ADR-0007 universe. ADR-0010 caveat 3 records this validated-universe vs trading-universe
> gap and names this backtest the next quant deliverable. The variants below run the LIVE
> book shape through the IDENTICAL gauntlet the deciles passed — verdicts land verbatim,
> pass or fail.

> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)
> Evaluation window 2012-07-02 → 2026-07-15 (>= 10 years); verdicts are decision-grade
> FOR THE IMPLEMENTABILITY QUESTION — pass or fail, recorded verbatim.

## Universe construction — the honesty section (read first)

### The point-in-time S&P 100 does NOT exist at the vendor

Probed live 2026-07-18: `fundamentals/OEX.INDX` returns only `General` and the 101
current `Components` — **no `HistoricalTickerComponents`** (the GSPC.INDX table that
built `validation.index_membership` has no OEX equivalent). A true point-in-time
S&P 100 therefore CANNOT be reconstructed from our vendor, and no third-party list
was fabricated in its place.

### The documented fallback (an APPROXIMATION, and why it is honest)

The point-in-time S&P 500 (fail-closed interval rule, delisted names included —
the `GSPC.INDX` membership the decile runs validated on) restricted at EACH
rebalance to the **top-100 names by trailing 63-session mean daily dollar
volume**. Properties:

- **Point-in-time**: the filter at rebalance t reads only sessions <= t (prefix sums
  over the panel; asserted in code). Dead mega-caps are eligible while they lived;
  a held name that dies is liquidated at its final close (delisting rule imported
  unchanged).
- **Deterministic**: tie-break (-dollar volume, symbol); cached per rebalance; the
  strategy, the monkey null and the equal-weight benchmark all read the identical set.
- **Dollar-volume basis (verified before use)**: EODHD stores bar volume already
  split-adjusted (AAPL 2020-08-28 stored x4, NVDA 2024-06-07 x10) while closes are
  raw; true traded dollars = split-adjusted close x stored volume (the factors
  cancel). The engine's OBar volume is double-adjusted and was NOT used.
- **It is an approximation**: the real S&P 100 is committee-chosen (options listing,
  sector balance) and cannot be reconstructed point-in-time. Cross-check where a
  check exists: at the final rebalance (2026-06-30) the filter's top-100
  overlaps the vendor's CURRENT S&P 100 components on **68/101** names.
  Historical overlap is unverifiable (that is exactly the missing data); this is
  stated, not hidden.
- **India ADRs are excluded**: the live ADR-0007 universe carries 5 India ADRs, but
  they hold no US index membership, so no honest point-in-time construction covers
  them. **This test validates (or fails) the US large-cap satellite only; the India
  sleeve remains unvalidated by construction.**

### Panel and coverage (inherited from the decile runs' loader, unchanged)

- Panel 2010-01-04 → 2026-07-15 (4157 aligned XNYS sessions), total-return convention on every
  series (dividends reinvested at the ex-date close; identical on both sides of
  every comparison)
- Members with usable series: 657 (70 delisted); missing series: 3; SPY carries 66 reinvested distributions (asserted non-zero)
- Earnings coverage: 637 members with >= 1 stored surprise (60109 reports; 53 delisted names)
- Rebalance-set sizes at the final rebalance (2026-06-30): 502 members / 494 eligible / 100 large-cap base / 100 with live SUE
- December snapshots (members/eligible/base/PEAD-base): 2012: 347/342/100/97; 2013: 366/360/100/96; 2014: 381/376/100/97; 2015: 403/395/100/98; 2016: 424/420/100/96; 2017: 445/443/100/100; 2018: 460/457/100/100; 2019: 475/468/100/100; 2020: 484/477/100/99; 2021: 493/486/100/100; 2022: 496/489/100/100; 2023: 501/495/100/100; 2024: 502/495/100/100; 2025: 502/496/100/100

## Method (everything imported, nothing restated)

- Construction: top-5 equal weight per sleeve (SLEEVE_MAX_NAMES — the live cap, imported from the production signal generators); monthly rebalance at month-end close, execution next session's open; costs 5+5 bps/side on turnover
- Combined satellite: 50/50 momentum+PEAD sleeves (ADR-0013 consequence 2), overlap sums, an empty sleeve holds cash
- Null model: 1000-path monkey MC, min(5, |eligible|) names drawn uniformly from the SAME cached eligible set(s) with the SAME budgets, identical engine/costs/delisting rule (ADR-0002 #2)
- Walk-forward: purged+embargoed, k=4, horizon=40, embargo=10, warmup = evaluation-start index (ADR-0002 #3)
- Deflated Sharpe at each family's true registered trial count (ADR-0002 #1); every run registered in quant.trial_registry
- Binding benchmark: SPY buy-and-hold TOTAL return over the same window (ADR-0009); SPY holds no membership row and can never be ranked
- Pre-committed kill-only trials: evaluation start 2016-01-01 (imported from the decile runs' board commitment) — they can only demote, never validate

## Variant 1 — `xsmom-impl-tr`: momentum 12-1, top-5 sleeve

Family `xsmom-impl-tr`; evaluation start 2012-07-02; 168 rebalances; forced delisting liquidations 5; unfilled buys 0.

Return +2201.86%, Sharpe 0.83, max drawdown -51.97%, avg turnover 68.88% per rebalance (sum |Δw|, both sides)

### Gate verdict: **PASS**

- verdict: **PASS**
- strategy TOTAL return: +2201.86%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +593.76%
- margin over SPY TR: +1608.10%
- equal-weight whole-eligible-base TR (informational, NOT binding): +620.13%
- null-model p-value: 0.000 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.999 at n_trials=1 (must be >= 0.9)
- trial registry id: `e6d584b0-27e1-47b9-9352-96f3d7be3f8e`

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +74.41% | +55.66% | +18.76% |
| 2 | +108.70% | +64.96% | +43.74% |
| 3 | +17.33% | +34.94% | -17.61% |
| 4 | +432.27% | +94.63% | +337.63% |

- mean return +158.18%, mean Sharpe 0.85, worst fold +17.33%

### Exhibit: verdict vs endpoint — Variant 1 — `xsmom-impl-tr`: momentum 12-1, top-5 sleeve

**25/25 endpoints beat SPY TR; 21/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +479.34% | +394.58% | +84.76% | 0.081 | 0.986 | FAIL |
| 2024-08-30 | +469.68% | +406.13% | +63.55% | 0.094 | 0.985 | FAIL |
| 2024-09-30 | +526.18% | +416.77% | +109.41% | 0.069 | 0.988 | FAIL |
| 2024-10-31 | +545.90% | +412.16% | +133.74% | 0.061 | 0.989 | FAIL |
| 2024-11-29 | +670.04% | +442.70% | +227.34% | 0.028 | 0.993 | PASS |
| 2024-12-31 | +709.43% | +429.62% | +279.81% | 0.016 | 0.993 | PASS |
| 2025-01-31 | +804.44% | +443.84% | +360.60% | 0.012 | 0.995 | PASS |
| 2025-02-28 | +709.37% | +436.94% | +272.44% | 0.022 | 0.993 | PASS |
| 2025-03-31 | +620.09% | +407.02% | +213.07% | 0.031 | 0.990 | PASS |
| 2025-04-30 | +738.16% | +402.62% | +335.53% | 0.014 | 0.992 | PASS |
| 2025-05-30 | +833.65% | +434.21% | +399.44% | 0.010 | 0.994 | PASS |
| 2025-06-30 | +888.02% | +461.67% | +426.35% | 0.011 | 0.995 | PASS |
| 2025-07-31 | +994.43% | +474.60% | +519.83% | 0.004 | 0.996 | PASS |
| 2025-08-29 | +957.13% | +486.39% | +470.74% | 0.007 | 0.996 | PASS |
| 2025-09-30 | +1047.29% | +507.27% | +540.02% | 0.004 | 0.996 | PASS |
| 2025-10-31 | +1030.36% | +521.75% | +508.62% | 0.010 | 0.996 | PASS |
| 2025-11-28 | +988.72% | +522.96% | +465.76% | 0.012 | 0.996 | PASS |
| 2025-12-31 | +955.82% | +523.44% | +432.38% | 0.013 | 0.995 | PASS |
| 2026-01-30 | +1245.99% | +532.63% | +713.36% | 0.004 | 0.997 | PASS |
| 2026-02-27 | +1316.06% | +527.16% | +788.89% | 0.003 | 0.998 | PASS |
| 2026-03-31 | +1205.31% | +496.22% | +709.09% | 0.004 | 0.997 | PASS |
| 2026-04-30 | +1847.33% | +558.85% | +1288.48% | 0.000 | 0.999 | PASS |
| 2026-05-29 | +2284.68% | +593.52% | +1691.16% | 0.000 | 0.999 | PASS |
| 2026-06-30 | +2521.39% | +586.37% | +1935.02% | 0.000 | 0.999 | PASS |
| 2026-07-15 | +2201.86% | +593.76% | +1608.10% | 0.000 | 0.999 | PASS |

## Variant 2 — `pead-impl-tr`: PEAD/SUE, top-5 sleeve

Family `pead-impl-tr`; evaluation start 2012-07-02; 168 rebalances; forced delisting liquidations 2; unfilled buys 0.

Return +723.94%, Sharpe 0.79, max drawdown -37.33%, avg turnover 81.70% per rebalance (sum |Δw|, both sides)

### Gate verdict: **FAIL**

- verdict: **FAIL**
- strategy TOTAL return: +723.94%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +593.76%
- margin over SPY TR: +130.18%
- equal-weight whole-eligible-base TR (informational, NOT binding): +636.05%
- null-model p-value: 0.132 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.998 at n_trials=1 (must be >= 0.9)
- trial registry id: `676ccff0-8dcb-45bc-9b00-82b7388336fa`

Verbatim gate reasons:
- null-model: p=0.132 > 0.05 (random same-universe portfolios do as well)

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +58.85% | +55.66% | +3.20% |
| 2 | +120.22% | +64.96% | +55.26% |
| 3 | +17.71% | +34.94% | -17.23% |
| 4 | +106.42% | +94.63% | +11.78% |

- mean return +75.80%, mean Sharpe 0.91, worst fold +17.71%

### Exhibit: verdict vs endpoint — Variant 2 — `pead-impl-tr`: PEAD/SUE, top-5 sleeve

**3/25 endpoints beat SPY TR; 0/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +356.32% | +394.58% | -38.25% | 0.263 | 0.992 | FAIL |
| 2024-08-30 | +343.67% | +406.13% | -62.46% | 0.314 | 0.991 | FAIL |
| 2024-09-30 | +342.56% | +416.77% | -74.21% | 0.342 | 0.991 | FAIL |
| 2024-10-31 | +325.61% | +412.16% | -86.55% | 0.377 | 0.990 | FAIL |
| 2024-11-29 | +349.41% | +442.70% | -93.28% | 0.385 | 0.991 | FAIL |
| 2024-12-31 | +340.82% | +429.62% | -88.80% | 0.356 | 0.991 | FAIL |
| 2025-01-31 | +345.42% | +443.84% | -98.43% | 0.394 | 0.991 | FAIL |
| 2025-02-28 | +344.90% | +436.94% | -92.04% | 0.392 | 0.991 | FAIL |
| 2025-03-31 | +317.29% | +407.02% | -89.74% | 0.389 | 0.988 | FAIL |
| 2025-04-30 | +331.66% | +402.62% | -70.96% | 0.367 | 0.989 | FAIL |
| 2025-05-30 | +357.02% | +434.21% | -77.19% | 0.365 | 0.991 | FAIL |
| 2025-06-30 | +361.01% | +461.67% | -100.66% | 0.410 | 0.991 | FAIL |
| 2025-07-31 | +359.47% | +474.60% | -115.14% | 0.420 | 0.991 | FAIL |
| 2025-08-29 | +360.71% | +486.39% | -125.68% | 0.430 | 0.991 | FAIL |
| 2025-09-30 | +429.70% | +507.27% | -77.57% | 0.326 | 0.994 | FAIL |
| 2025-10-31 | +476.44% | +521.75% | -45.31% | 0.275 | 0.996 | FAIL |
| 2025-11-28 | +444.24% | +522.96% | -78.72% | 0.315 | 0.994 | FAIL |
| 2025-12-31 | +450.95% | +523.44% | -72.50% | 0.312 | 0.995 | FAIL |
| 2026-01-30 | +421.11% | +532.63% | -111.52% | 0.377 | 0.993 | FAIL |
| 2026-02-27 | +409.35% | +527.16% | -117.81% | 0.403 | 0.993 | FAIL |
| 2026-03-31 | +405.54% | +496.22% | -90.68% | 0.351 | 0.992 | FAIL |
| 2026-04-30 | +540.05% | +558.85% | -18.80% | 0.253 | 0.997 | FAIL |
| 2026-05-29 | +708.15% | +593.52% | +114.62% | 0.157 | 0.998 | FAIL |
| 2026-06-30 | +696.33% | +586.37% | +109.97% | 0.167 | 0.998 | FAIL |
| 2026-07-15 | +723.94% | +593.76% | +130.18% | 0.132 | 0.998 | FAIL |

## Variant 3 — `combined-impl-tr`: the actual satellite: 50/50 momentum+PEAD top-5 sleeves

Family `combined-impl-tr`; evaluation start 2012-07-02; 168 rebalances; forced delisting liquidations 7; unfilled buys 0.

Return +1403.58%, Sharpe 0.88, max drawdown -37.14%, avg turnover 73.93% per rebalance (sum |Δw|, both sides)

### Gate verdict: **PASS**

- verdict: **PASS**
- strategy TOTAL return: +1403.58%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +593.76%
- margin over SPY TR: +809.82%
- equal-weight whole-eligible-base TR (informational, NOT binding): +628.47%
- null-model p-value: 0.000 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 1.000 at n_trials=1 (must be >= 0.9)
- trial registry id: `b51d9d00-052e-464b-a973-55a17cf66dc2`

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +68.47% | +55.66% | +12.81% |
| 2 | +117.03% | +64.96% | +52.07% |
| 3 | +20.21% | +34.94% | -14.73% |
| 4 | +245.17% | +94.63% | +150.54% |

- mean return +112.72%, mean Sharpe 0.95, worst fold +20.21%

### Exhibit: verdict vs endpoint — Variant 3 — `combined-impl-tr`: the actual satellite: 50/50 momentum+PEAD top-5 sleeves

**25/25 endpoints beat SPY TR; 20/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +447.08% | +394.58% | +52.50% | 0.063 | 0.994 | FAIL |
| 2024-08-30 | +434.93% | +406.13% | +28.80% | 0.090 | 0.993 | FAIL |
| 2024-09-30 | +460.89% | +416.77% | +44.12% | 0.077 | 0.994 | FAIL |
| 2024-10-31 | +458.98% | +412.16% | +46.82% | 0.075 | 0.994 | FAIL |
| 2024-11-29 | +528.28% | +442.70% | +85.58% | 0.039 | 0.996 | PASS |
| 2024-12-31 | +538.39% | +429.62% | +108.77% | 0.025 | 0.996 | PASS |
| 2025-01-31 | +579.16% | +443.84% | +135.31% | 0.020 | 0.996 | PASS |
| 2025-02-28 | +542.25% | +436.94% | +105.31% | 0.043 | 0.995 | PASS |
| 2025-03-31 | +486.86% | +407.02% | +79.84% | 0.051 | 0.994 | FAIL |
| 2025-04-30 | +544.91% | +402.62% | +142.29% | 0.017 | 0.995 | PASS |
| 2025-05-30 | +600.90% | +434.21% | +166.69% | 0.015 | 0.996 | PASS |
| 2025-06-30 | +624.35% | +461.67% | +162.68% | 0.022 | 0.996 | PASS |
| 2025-07-31 | +662.16% | +474.60% | +187.56% | 0.016 | 0.997 | PASS |
| 2025-08-29 | +649.89% | +486.39% | +163.50% | 0.020 | 0.997 | PASS |
| 2025-09-30 | +738.03% | +507.27% | +230.75% | 0.012 | 0.998 | PASS |
| 2025-10-31 | +768.80% | +521.75% | +247.05% | 0.008 | 0.998 | PASS |
| 2025-11-28 | +728.46% | +522.96% | +205.50% | 0.013 | 0.997 | PASS |
| 2025-12-31 | +720.91% | +523.44% | +197.46% | 0.018 | 0.997 | PASS |
| 2026-01-30 | +810.29% | +532.63% | +277.66% | 0.006 | 0.998 | PASS |
| 2026-02-27 | +822.81% | +527.16% | +295.64% | 0.005 | 0.998 | PASS |
| 2026-03-31 | +783.05% | +496.22% | +286.83% | 0.005 | 0.998 | PASS |
| 2026-04-30 | +1116.70% | +558.85% | +557.85% | 0.001 | 0.999 | PASS |
| 2026-05-29 | +1411.87% | +593.52% | +818.35% | 0.000 | 1.000 | PASS |
| 2026-06-30 | +1475.88% | +586.37% | +889.51% | 0.000 | 1.000 | PASS |
| 2026-07-15 | +1403.58% | +593.76% | +809.82% | 0.000 | 1.000 | PASS |

# Pre-committed 2016 kill-only trials (demote-only)

Identical recipes, evaluation start 2016-01-01: they remove the biased early-membership window and the 2012-2015 head start. A PASS validates nothing by itself; a FAIL is a strike.

## Kill 1 — `xsmom-impl-tr-2016`: momentum 12-1, top-5 sleeve

Family `xsmom-impl-tr-2016`; evaluation start 2016-01-04; 126 rebalances; forced delisting liquidations 5; unfilled buys 0.

Return +1282.22%, Sharpe 0.86, max drawdown -51.97%, avg turnover 67.32% per rebalance (sum |Δw|, both sides)

### Gate verdict: **PASS**

- verdict: **PASS**
- strategy TOTAL return: +1282.22%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +364.89%
- margin over SPY TR: +917.33%
- equal-weight whole-eligible-base TR (informational, NOT binding): +374.78%
- null-model p-value: 0.000 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.997 at n_trials=1 (must be >= 0.9)
- trial registry id: `c2365e3f-4cfc-44b2-ae78-7efd83306d11`

### Walk-forward: 3/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +80.98% | +54.79% | +26.19% |
| 2 | +69.29% | +47.44% | +21.85% |
| 3 | -7.95% | +11.32% | -19.27% |
| 4 | +366.11% | +71.12% | +294.98% |

- mean return +127.11%, mean Sharpe 0.83, worst fold -7.95%

### Exhibit: verdict vs endpoint — Kill 1 — `xsmom-impl-tr-2016`: momentum 12-1, top-5 sleeve

**25/25 endpoints beat SPY TR; 20/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +247.88% | +231.42% | +16.46% | 0.145 | 0.963 | FAIL |
| 2024-08-30 | +242.08% | +239.16% | +2.92% | 0.170 | 0.961 | FAIL |
| 2024-09-30 | +276.01% | +246.29% | +29.72% | 0.121 | 0.968 | FAIL |
| 2024-10-31 | +287.85% | +243.20% | +44.65% | 0.099 | 0.970 | FAIL |
| 2024-11-29 | +362.39% | +263.67% | +98.73% | 0.050 | 0.980 | PASS |
| 2024-12-31 | +386.05% | +254.90% | +131.15% | 0.030 | 0.982 | PASS |
| 2025-01-31 | +443.10% | +264.43% | +178.67% | 0.016 | 0.985 | PASS |
| 2025-02-28 | +386.01% | +259.81% | +126.21% | 0.042 | 0.980 | PASS |
| 2025-03-31 | +332.40% | +239.76% | +92.64% | 0.058 | 0.973 | FAIL |
| 2025-04-30 | +403.30% | +236.81% | +166.49% | 0.023 | 0.980 | PASS |
| 2025-05-30 | +460.64% | +257.98% | +202.66% | 0.017 | 0.984 | PASS |
| 2025-06-30 | +493.29% | +276.38% | +216.91% | 0.015 | 0.986 | PASS |
| 2025-07-31 | +557.18% | +285.04% | +272.14% | 0.006 | 0.989 | PASS |
| 2025-08-29 | +534.79% | +292.95% | +241.84% | 0.012 | 0.988 | PASS |
| 2025-09-30 | +588.93% | +306.94% | +281.99% | 0.005 | 0.990 | PASS |
| 2025-10-31 | +578.76% | +316.64% | +262.12% | 0.009 | 0.990 | PASS |
| 2025-11-28 | +553.76% | +317.45% | +236.31% | 0.012 | 0.988 | PASS |
| 2025-12-31 | +534.00% | +317.77% | +216.23% | 0.017 | 0.987 | PASS |
| 2026-01-30 | +708.24% | +323.93% | +384.31% | 0.003 | 0.993 | PASS |
| 2026-02-27 | +750.31% | +320.27% | +430.05% | 0.000 | 0.993 | PASS |
| 2026-03-31 | +683.81% | +299.53% | +384.29% | 0.001 | 0.991 | PASS |
| 2026-04-30 | +1069.33% | +341.50% | +727.83% | 0.000 | 0.997 | PASS |
| 2026-05-29 | +1331.95% | +364.73% | +967.22% | 0.000 | 0.998 | PASS |
| 2026-06-30 | +1474.09% | +359.94% | +1114.16% | 0.000 | 0.998 | PASS |
| 2026-07-15 | +1282.22% | +364.89% | +917.33% | 0.000 | 0.997 | PASS |

## Kill 2 — `pead-impl-tr-2016`: PEAD/SUE, top-5 sleeve

Family `pead-impl-tr-2016`; evaluation start 2016-01-04; 126 rebalances; forced delisting liquidations 2; unfilled buys 0.

Return +459.39%, Sharpe 0.80, max drawdown -37.33%, avg turnover 82.89% per rebalance (sum |Δw|, both sides)

### Gate verdict: **FAIL**

- verdict: **FAIL**
- strategy TOTAL return: +459.39%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +364.89%
- margin over SPY TR: +94.50%
- equal-weight whole-eligible-base TR (informational, NOT binding): +384.55%
- null-model p-value: 0.139 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.995 at n_trials=1 (must be >= 0.9)
- trial registry id: `d29ef39a-a3ce-4b92-bf59-85e711285889`

Verbatim gate reasons:
- null-model: p=0.139 > 0.05 (random same-universe portfolios do as well)

### Walk-forward: 3/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +89.10% | +54.79% | +34.31% |
| 2 | +43.78% | +47.44% | -3.66% |
| 3 | -18.17% | +11.32% | -29.49% |
| 4 | +137.36% | +71.12% | +66.23% |

- mean return +63.02%, mean Sharpe 0.86, worst fold -18.17%

### Exhibit: verdict vs endpoint — Kill 2 — `pead-impl-tr-2016`: PEAD/SUE, top-5 sleeve

**3/25 endpoints beat SPY TR; 0/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +209.81% | +231.42% | -21.61% | 0.285 | 0.977 | FAIL |
| 2024-08-30 | +201.22% | +239.16% | -37.94% | 0.342 | 0.974 | FAIL |
| 2024-09-30 | +200.47% | +246.29% | -45.82% | 0.373 | 0.974 | FAIL |
| 2024-10-31 | +188.95% | +243.20% | -54.24% | 0.408 | 0.970 | FAIL |
| 2024-11-29 | +205.12% | +263.67% | -58.55% | 0.412 | 0.975 | FAIL |
| 2024-12-31 | +199.29% | +254.90% | -55.61% | 0.384 | 0.973 | FAIL |
| 2025-01-31 | +202.40% | +264.43% | -62.03% | 0.414 | 0.973 | FAIL |
| 2025-02-28 | +202.05% | +259.81% | -57.75% | 0.421 | 0.973 | FAIL |
| 2025-03-31 | +183.31% | +239.76% | -56.45% | 0.421 | 0.966 | FAIL |
| 2025-04-30 | +193.07% | +236.81% | -43.74% | 0.381 | 0.968 | FAIL |
| 2025-05-30 | +210.28% | +257.98% | -47.69% | 0.380 | 0.973 | FAIL |
| 2025-06-30 | +212.99% | +276.38% | -63.39% | 0.446 | 0.974 | FAIL |
| 2025-07-31 | +211.94% | +285.04% | -73.10% | 0.453 | 0.973 | FAIL |
| 2025-08-29 | +212.79% | +292.95% | -80.16% | 0.455 | 0.973 | FAIL |
| 2025-09-30 | +259.63% | +306.94% | -47.31% | 0.354 | 0.983 | FAIL |
| 2025-10-31 | +291.36% | +316.64% | -25.28% | 0.290 | 0.987 | FAIL |
| 2025-11-28 | +269.50% | +317.45% | -47.95% | 0.344 | 0.984 | FAIL |
| 2025-12-31 | +274.05% | +317.77% | -43.72% | 0.337 | 0.984 | FAIL |
| 2026-01-30 | +253.79% | +323.93% | -70.14% | 0.418 | 0.981 | FAIL |
| 2026-02-27 | +245.81% | +320.27% | -74.45% | 0.427 | 0.979 | FAIL |
| 2026-03-31 | +243.22% | +299.53% | -56.31% | 0.383 | 0.978 | FAIL |
| 2026-04-30 | +334.55% | +341.50% | -6.95% | 0.270 | 0.990 | FAIL |
| 2026-05-29 | +448.67% | +364.73% | +83.94% | 0.161 | 0.995 | FAIL |
| 2026-06-30 | +440.65% | +359.94% | +80.71% | 0.178 | 0.995 | FAIL |
| 2026-07-15 | +459.39% | +364.89% | +94.50% | 0.139 | 0.995 | FAIL |

## Kill 3 — `combined-impl-tr-2016`: the actual satellite: 50/50 momentum+PEAD top-5 sleeves

Family `combined-impl-tr-2016`; evaluation start 2016-01-04; 126 rebalances; forced delisting liquidations 7; unfilled buys 0.

Return +848.51%, Sharpe 0.91, max drawdown -37.14%, avg turnover 73.85% per rebalance (sum |Δw|, both sides)

### Gate verdict: **PASS**

- verdict: **PASS**
- strategy TOTAL return: +848.51%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +364.89%
- margin over SPY TR: +483.62%
- equal-weight whole-eligible-base TR (informational, NOT binding): +379.85%
- null-model p-value: 0.000 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.998 at n_trials=1 (must be >= 0.9)
- trial registry id: `1469f323-af95-40ef-b7b8-3e4a79741882`

### Walk-forward: 3/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +86.71% | +54.79% | +31.92% |
| 2 | +58.19% | +47.44% | +10.75% |
| 3 | -11.79% | +11.32% | -23.11% |
| 4 | +244.64% | +71.12% | +173.51% |

- mean return +94.44%, mean Sharpe 0.91, worst fold -11.79%

### Exhibit: verdict vs endpoint — Kill 3 — `combined-impl-tr-2016`: the actual satellite: 50/50 momentum+PEAD top-5 sleeves

**24/25 endpoints beat SPY TR; 13/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +245.12% | +231.42% | +13.70% | 0.130 | 0.979 | FAIL |
| 2024-08-30 | +237.45% | +239.16% | -1.71% | 0.159 | 0.976 | FAIL |
| 2024-09-30 | +253.83% | +246.29% | +7.54% | 0.148 | 0.979 | FAIL |
| 2024-10-31 | +252.62% | +243.20% | +9.43% | 0.146 | 0.979 | FAIL |
| 2024-11-29 | +296.34% | +263.67% | +32.68% | 0.100 | 0.985 | FAIL |
| 2024-12-31 | +302.72% | +254.90% | +47.82% | 0.059 | 0.986 | FAIL |
| 2025-01-31 | +328.44% | +264.43% | +64.00% | 0.055 | 0.988 | FAIL |
| 2025-02-28 | +305.16% | +259.81% | +45.35% | 0.089 | 0.985 | FAIL |
| 2025-03-31 | +270.21% | +239.76% | +30.46% | 0.110 | 0.979 | FAIL |
| 2025-04-30 | +306.84% | +236.81% | +70.02% | 0.051 | 0.983 | FAIL |
| 2025-05-30 | +342.16% | +257.98% | +84.18% | 0.042 | 0.987 | PASS |
| 2025-06-30 | +356.95% | +276.38% | +80.57% | 0.054 | 0.988 | FAIL |
| 2025-07-31 | +380.80% | +285.04% | +95.76% | 0.039 | 0.990 | PASS |
| 2025-08-29 | +373.06% | +292.95% | +80.11% | 0.051 | 0.989 | FAIL |
| 2025-09-30 | +428.66% | +306.94% | +121.72% | 0.028 | 0.993 | PASS |
| 2025-10-31 | +448.07% | +316.64% | +131.43% | 0.026 | 0.993 | PASS |
| 2025-11-28 | +422.62% | +317.45% | +105.17% | 0.039 | 0.992 | PASS |
| 2025-12-31 | +417.86% | +317.77% | +100.09% | 0.047 | 0.991 | PASS |
| 2026-01-30 | +474.25% | +323.93% | +150.32% | 0.023 | 0.994 | PASS |
| 2026-02-27 | +482.14% | +320.27% | +161.88% | 0.021 | 0.994 | PASS |
| 2026-03-31 | +457.06% | +299.53% | +157.53% | 0.021 | 0.992 | PASS |
| 2026-04-30 | +667.54% | +341.50% | +326.04% | 0.003 | 0.997 | PASS |
| 2026-05-29 | +853.75% | +364.73% | +489.01% | 0.001 | 0.999 | PASS |
| 2026-06-30 | +894.12% | +359.94% | +534.18% | 0.000 | 0.999 | PASS |
| 2026-07-15 | +848.51% | +364.89% | +483.62% | 0.000 | 0.998 | PASS |

### Exhibit: per-calendar-year total returns (full-window variants vs SPY TR)

| year | xsmom-impl | pead-impl | combined-impl | SPY TR |
|---|---|---|---|---|
| 2012 | +10.44% | +7.02% | +8.89% | +3.86% |
| 2013 | +56.92% | +39.08% | +48.54% | +32.31% |
| 2014 | +18.93% | +14.32% | +16.93% | +13.46% |
| 2015 | -12.24% | -5.02% | -8.51% | +1.25% |
| 2016 | +16.23% | +10.16% | +13.43% | +12.00% |
| 2017 | +23.97% | +32.85% | +28.62% | +21.70% |
| 2018 | -2.41% | +8.78% | +3.66% | -4.56% |
| 2019 | +49.80% | +30.75% | +40.51% | +31.22% |
| 2020 | +50.03% | +19.74% | +34.67% | +18.37% |
| 2021 | -21.39% | +21.78% | -1.39% | +28.75% |
| 2022 | -3.13% | -20.85% | -11.74% | -18.17% |
| 2023 | +22.32% | -7.04% | +7.18% | +26.19% |
| 2024 | +52.02% | +22.14% | +38.18% | +24.89% |
| 2025 | +30.44% | +24.98% | +28.59% | +17.72% |
| 2026 | +118.02% | +49.55% | +83.16% | +11.28% |

## Summary

| trial | window | strategy TR | SPY TR | margin | null p | DSR (n) | WF+ | endpoints beat/pass | verdict |
|---|---|---|---|---|---|---|---|---|---|
| `xsmom-impl-tr` | 2012-07-02 → 2026-07-15 | +2201.86% | +593.76% | +1608.10% | 0.000 | 0.999 (1) | 4/4 | 25/21/25 | **PASS** |
| `pead-impl-tr` | 2012-07-02 → 2026-07-15 | +723.94% | +593.76% | +130.18% | 0.132 | 0.998 (1) | 4/4 | 3/0/25 | **FAIL** |
| `combined-impl-tr` | 2012-07-02 → 2026-07-15 | +1403.58% | +593.76% | +809.82% | 0.000 | 1.000 (1) | 4/4 | 25/20/25 | **PASS** |
| `xsmom-impl-tr-2016` | 2016-01-04 → 2026-07-15 | +1282.22% | +364.89% | +917.33% | 0.000 | 0.997 (1) | 3/4 | 25/20/25 | **PASS** |
| `pead-impl-tr-2016` | 2016-01-04 → 2026-07-15 | +459.39% | +364.89% | +94.50% | 0.139 | 0.995 (1) | 3/4 | 3/0/25 | **FAIL** |
| `combined-impl-tr-2016` | 2016-01-04 → 2026-07-15 | +848.51% | +364.89% | +483.62% | 0.000 | 0.998 (1) | 3/4 | 24/13/25 | **PASS** |

Trial registry: **35 trials before this run → 41 after** (six trials: three full-window families, three pre-committed kills).

## What this means for the LIVE sleeves

The combined-satellite variant — the closest construction to what the fund actually holds — CLEARED the binding bar on the honest point-in-time large-cap universe. The validated-universe vs trading-universe gap of ADR-0010 caveat 3 is closed to the extent this approximation allows (the S&P 100 approximation and the excluded India sleeve are the remaining daylight, both documented above). Per-variant verdicts above stand on their own.

**However: the standalone `pead-impl-tr` sleeve FAILED its own gate** (null p=0.132; 3/25 endpoints beat SPY TR). At top-5 concentration its ranking is indistinguishable from drawing names at random from the same eligible set — the combined PASS is carried by the other sleeve, not by this one. Whether this sleeve keeps its own live budget is a PRINCIPAL DECISION on this evidence: its decile validation does not transfer to the live book shape on its own.

Caveats that survive any verdict: (1) the universe is an approximation of the S&P 100, documented above; (2) the India ADR sleeve is untested by construction; (3) the early-window membership undercount that flattered the decile runs flatters these runs identically; (4) endpoint concentration must be read from the exhibits, not assumed away; (5) the board-memo item-5 overlay (ADR-0006 2xATR stops, L9 staggered entries, L5 gross caps, small-account frictions beyond 10 bps/side) is NOT modeled — this run isolates the universe + top-5 concentration question, and the stop-overlaid configuration still has no backtest evidence of its own.

## Reproduction

Deterministic re-run (official registration against the dev database, after review):

```bash
python -m atlas.dcp.backtest.impl_variant_run --paths 1000 --seed 7 --window-end 2026-07-15
```

The `--window-end` pin makes the run byte-identical even after later nightly ingests extend the stored history.

## Approval status

**None sought here — by design.** This is a VALIDATION run on the membership-gated universe (validation-only instruments); it does not qualify or disqualify any strategy row by itself. Gates were not modified; verdicts are recorded verbatim; what happens to the live sleeves is a Principal decision made on this evidence.
