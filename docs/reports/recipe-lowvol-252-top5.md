# RECIPE GAUNTLET — `recipe-lowvol-252-top5` (low_vol_252, top-5, monthly, pit-sp500)

> ## WHAT THIS IS
> A spec-driven run of the committed portfolio gauntlet (Research Factory v1).
> Everything is imported from the validated runners — engine, eligibility, null
> model, thresholds, walk-forward, delisting rule — and the ranking values come
> from the point-in-time FEATURE STORE, whose equivalence to the production
> signal math is golden-pinned. Verdicts land verbatim, pass or fail.

## The spec (frozen; registered verbatim with both trials)

- spec_hash: `e57b0f87221e3ca699e42a1529cd78345d7b8d214c23a1b6e7cb0eed1334da45`
- name: `lowvol-252-top5`; rank_feature: `low_vol_252`; direction: desc; top_n: 5; rebalance: monthly; universe: pit-sp500
- costs: 10 bps/side, FIXED (the committed CostModel — never a free parameter)
- lineage: `low-vol` (ADR-0016 — the deflation count basis)
- rationale (registered as the trial hypothesis, pre-run): Low-volatility anomaly: leverage- and benchmark-constrained investors overpay for lottery-like high-volatility names, so defensive stocks have earned more than their risk due (Ang et al. 2006; Baker-Bradley-Wurgler 2011; Frazzini-Pedersen 2014). Hypothesis: ranking the point-in-time S&P 500 defensive-first (lowest realized 252-day vol) at top-5/monthly survives costs, the null model, deflated Sharpe and walk-forward — and clears Atlas's absolute beat-SPY-TR bar. The literature's claim is risk-adjusted, so failing the absolute bar is a live possibility; either verdict is worth the counted burn.
- pre-committed kill start: 2016-01-01 (demote-only)
- dataset_version: `d4bac02c4c7c8592701e5c98d69eed49afa3a5ba595cd97c4ff1fa0eefeab609` (the feature store's input-vintage pin)
- ranking basis: stored feature values: split-adjusted PRICE closes — the production signal generator's basis (features/momentum.py, equivalence-pinned); accounting and benchmark are total-return per ADR-0009
- return convention: total return (split-adjusted; each dividend reinvested at its ex-date close — market_data/total_return.py)

## Panel and coverage (loader inherited unchanged)

- Panel 2010-01-04 → 2026-07-17; members with usable series: 657 (70 delisted); missing series: 3; SPY carries 66 reinvested distributions (asserted non-zero)
- Feature materialization: 198 rebalance sessions, 104299 values inserted, 0 already present, 0 failures (fail-loud)
- Null model: 1000-path seeded monkey MC (ADR-0002 #2); walk-forward k=4, horizon=40, embargo=10 (real_run constants, ADR-0002 #3)

## Trial 1 — `recipe-lowvol-252-top5`: the recipe on its full window

Family `recipe-lowvol-252-top5`; evaluation start 2012-07-02; 168 rebalances; forced delisting liquidations 2; unfilled buys 0.

Return +189.49%, Sharpe 0.58, max drawdown -36.08%, avg turnover 44.19% per rebalance (sum |Δw|, both sides)

### Gate verdict: **FAIL**

- verdict: **FAIL**
- strategy TOTAL return: +189.49%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +583.17%
- margin over SPY TR: -393.67%
- equal-weight all-eligible TR (informational, NOT binding): +511.27%
- null-model p-value: 0.791 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.986 at n_trials=1 (lineage 'low-vol', 1 registered trials; must be >= 0.9)
- trial registry id: `705d8380-bbf4-4fea-8697-b2aac426d9fc` (registered and COMMITTED before the run; metrics enriched on the same row after)

Verbatim gate reasons:
- null-model: p=0.791 > 0.05 (random same-universe portfolios do as well)
- does not beat SPY buy-and-hold (189.5% <= 583.2%)

### Walk-forward: 4/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +34.98% | +55.66% | -20.68% |
| 2 | +46.70% | +64.96% | -18.26% |
| 3 | +8.20% | +34.94% | -26.74% |
| 4 | +37.30% | +91.66% | -54.36% |

- mean return +31.80%, mean Sharpe 0.73, worst fold +8.20%

### Exhibit: verdict vs endpoint — Trial 1 — `recipe-lowvol-252-top5`: the recipe on its full window

**0/25 endpoints beat SPY TR; 0/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +121.50% | +394.58% | -273.08% | 0.889 | 0.962 | FAIL |
| 2024-08-30 | +137.23% | +406.13% | -268.90% | 0.865 | 0.971 | FAIL |
| 2024-09-30 | +134.17% | +416.77% | -282.59% | 0.881 | 0.970 | FAIL |
| 2024-10-31 | +127.92% | +412.16% | -284.24% | 0.885 | 0.966 | FAIL |
| 2024-11-29 | +139.46% | +442.70% | -303.23% | 0.888 | 0.972 | FAIL |
| 2024-12-31 | +123.31% | +429.62% | -306.31% | 0.885 | 0.963 | FAIL |
| 2025-01-31 | +133.43% | +443.84% | -310.41% | 0.878 | 0.969 | FAIL |
| 2025-02-28 | +151.95% | +436.94% | -284.99% | 0.833 | 0.977 | FAIL |
| 2025-03-31 | +155.01% | +407.02% | -252.01% | 0.796 | 0.978 | FAIL |
| 2025-04-30 | +150.88% | +402.62% | -251.74% | 0.788 | 0.976 | FAIL |
| 2025-05-30 | +142.74% | +434.21% | -291.47% | 0.845 | 0.972 | FAIL |
| 2025-06-30 | +146.37% | +461.67% | -315.30% | 0.851 | 0.974 | FAIL |
| 2025-07-31 | +149.20% | +474.60% | -325.41% | 0.844 | 0.975 | FAIL |
| 2025-08-29 | +151.93% | +486.39% | -334.46% | 0.854 | 0.976 | FAIL |
| 2025-09-30 | +160.45% | +507.27% | -346.82% | 0.836 | 0.979 | FAIL |
| 2025-10-31 | +162.20% | +521.75% | -359.55% | 0.821 | 0.979 | FAIL |
| 2025-11-28 | +167.06% | +522.96% | -355.90% | 0.813 | 0.981 | FAIL |
| 2025-12-31 | +156.81% | +523.44% | -366.63% | 0.842 | 0.977 | FAIL |
| 2026-01-30 | +168.75% | +532.63% | -363.88% | 0.818 | 0.981 | FAIL |
| 2026-02-27 | +192.58% | +527.16% | -334.58% | 0.782 | 0.987 | FAIL |
| 2026-03-31 | +190.02% | +496.22% | -306.20% | 0.730 | 0.986 | FAIL |
| 2026-04-30 | +188.92% | +558.85% | -369.93% | 0.774 | 0.986 | FAIL |
| 2026-05-29 | +177.63% | +593.52% | -415.89% | 0.810 | 0.983 | FAIL |
| 2026-06-30 | +190.27% | +586.37% | -396.10% | 0.789 | 0.986 | FAIL |
| 2026-07-17 | +189.49% | +583.17% | -393.67% | 0.791 | 0.986 | FAIL |

# Pre-committed kill trial (demote-only)

Identical recipe, evaluation start 2016-01-01 — pre-committed in the spec BEFORE any result existed. A PASS here validates nothing by itself; a FAIL is a strike.

## Trial 2 — `recipe-lowvol-252-top5-2016`: the kill window

Family `recipe-lowvol-252-top5-2016`; evaluation start 2016-01-04; 126 rebalances; forced delisting liquidations 2; unfilled buys 0.

Return +113.94%, Sharpe 0.53, max drawdown -36.08%, avg turnover 43.54% per rebalance (sum |Δw|, both sides)

### Gate verdict: **FAIL**

- verdict: **FAIL**
- strategy TOTAL return: +113.94%
- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): +357.80%
- margin over SPY TR: -243.86%
- equal-weight all-eligible TR (informational, NOT binding): +283.88%
- null-model p-value: 0.759 (must be <= 0.05) — monkeys draw 5 names from the identical eligible set with the identical construction
- deflated Sharpe: 0.887 at n_trials=2 (lineage 'low-vol', 2 registered trials; must be >= 0.9)
- trial registry id: `e7b70d16-95ad-4b52-a0e5-5300bfaf5c54` (registered and COMMITTED before the run; metrics enriched on the same row after)

Verbatim gate reasons:
- null-model: p=0.759 > 0.05 (random same-universe portfolios do as well)
- does not beat SPY buy-and-hold (113.9% <= 357.8%)
- deflated Sharpe 0.89 < 0.9 at n_trials=2

### Walk-forward: 3/4 folds positive — with SPY through the identical fold machinery

| fold | strategy TR | SPY TR (same fold) | strategy − SPY |
|---|---|---|---|
| 1 | +29.81% | +55.33% | -25.52% |
| 2 | +26.36% | +47.52% | -21.17% |
| 3 | -9.29% | +12.07% | -21.36% |
| 4 | +44.33% | +68.51% | -24.18% |

- mean return +22.80%, mean Sharpe 0.62, worst fold -9.29%

### Exhibit: verdict vs endpoint — Trial 2 — `recipe-lowvol-252-top5-2016`: the kill window

**0/25 endpoints beat SPY TR; 0/25 endpoints PASS the full gate.** (final date rolled back to each of the prior 24 month-ends; exact truncation of the stored strategy/SPY/null curves)

| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |
|---|---|---|---|---|---|---|
| 2024-07-31 | +63.68% | +231.42% | -167.74% | 0.866 | 0.771 | FAIL |
| 2024-08-30 | +75.31% | +239.16% | -163.85% | 0.832 | 0.811 | FAIL |
| 2024-09-30 | +73.05% | +246.29% | -173.24% | 0.850 | 0.803 | FAIL |
| 2024-10-31 | +68.43% | +243.20% | -174.77% | 0.856 | 0.787 | FAIL |
| 2024-11-29 | +76.96% | +263.67% | -186.70% | 0.857 | 0.815 | FAIL |
| 2024-12-31 | +65.03% | +254.90% | -189.87% | 0.863 | 0.774 | FAIL |
| 2025-01-31 | +72.51% | +264.43% | -191.93% | 0.851 | 0.799 | FAIL |
| 2025-02-28 | +86.19% | +259.81% | -173.62% | 0.795 | 0.840 | FAIL |
| 2025-03-31 | +88.45% | +239.76% | -151.31% | 0.745 | 0.845 | FAIL |
| 2025-04-30 | +85.40% | +236.81% | -151.41% | 0.735 | 0.834 | FAIL |
| 2025-05-30 | +79.38% | +257.98% | -178.60% | 0.802 | 0.816 | FAIL |
| 2025-06-30 | +82.06% | +276.38% | -194.31% | 0.812 | 0.823 | FAIL |
| 2025-07-31 | +84.15% | +285.04% | -200.89% | 0.812 | 0.829 | FAIL |
| 2025-08-29 | +86.18% | +292.95% | -206.77% | 0.829 | 0.834 | FAIL |
| 2025-09-30 | +92.47% | +306.94% | -214.47% | 0.801 | 0.849 | FAIL |
| 2025-10-31 | +93.77% | +316.64% | -222.87% | 0.782 | 0.852 | FAIL |
| 2025-11-28 | +97.36% | +317.45% | -220.09% | 0.787 | 0.860 | FAIL |
| 2025-12-31 | +89.78% | +317.77% | -227.99% | 0.813 | 0.842 | FAIL |
| 2026-01-30 | +98.60% | +323.93% | -225.33% | 0.788 | 0.862 | FAIL |
| 2026-02-27 | +116.22% | +320.27% | -204.05% | 0.740 | 0.894 | FAIL |
| 2026-03-31 | +114.32% | +299.53% | -185.21% | 0.679 | 0.890 | FAIL |
| 2026-04-30 | +113.51% | +341.50% | -227.99% | 0.733 | 0.888 | FAIL |
| 2026-05-29 | +105.17% | +364.73% | -259.57% | 0.780 | 0.872 | FAIL |
| 2026-06-30 | +114.50% | +359.94% | -245.43% | 0.752 | 0.888 | FAIL |
| 2026-07-17 | +113.94% | +357.80% | -243.86% | 0.759 | 0.887 | FAIL |

## Summary

| trial | window | strategy TR | SPY TR | margin | null p | DSR (n) | WF+ | endpoints beat/pass/total | verdict |
|---|---|---|---|---|---|---|---|---|---|
| `recipe-lowvol-252-top5` | 2012-07-02 → 2026-07-17 | +189.49% | +583.17% | -393.67% | 0.791 | 0.986 (1) | 4/4 | 0/0/25 | **FAIL** |
| `recipe-lowvol-252-top5-2016` | 2016-01-04 → 2026-07-17 | +113.94% | +357.80% | -243.86% | 0.759 | 0.887 (2) | 3/4 | 0/0/25 | **FAIL** |

Trial registry: **49 trials before this run → 51 after** (two pre-committed registrations; lineage 'low-vol' count now 2).

## Approval status

**None sought here — by design.** A recipe PASS only means the recipe may be taken to the separate approval workflow (dcp/backtest/approval.py) by the Principal; the gates were not modified and no strategy row is touched.
