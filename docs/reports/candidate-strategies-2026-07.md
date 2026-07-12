# Candidate strategies — trend / meanrev / breakout v1 (2026-07)

> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)
> This evaluation runs on the **full vendor history** (2010-01-04 → 2026-07-10).
> Verdicts below are decision-grade: an approval decision MAY rest on
> them — pass or fail, recorded verbatim.

Three classic families at textbook parameters — cited in each module
docstring, chosen without any parameter search — evaluated through the
UNMODIFIED gates. Nothing was tuned for this run (CLAUDE.md: a failing
gate on real data is a valid, reportable result). One registered trial
per (family, symbol).

- Engine: event-driven, next-open entry, costs 5.0+5.0 bps/side
- Null model: 1000-path random-entry MC, identical exits and costs (ADR-0002 #2)
- Walk-forward: purged+embargoed, k=4, horizon=40, embargo=10, warmup=60 (ADR-0002 #3)
- Every run registered in quant.trial_registry; deflated Sharpe uses the true family trial count (ADR-0002 #1)

## Graveyard context

momentum v1 (trend_rs_vol) **FAILED the gates on real data** — SPY and AVGO, on the 1y window (`docs/reports/first-real-backtest-momentum-v1.md`) and again decision-grade on the full 2010→2026 history (`docs/reports/decision-grade-momentum-v1.md`); the momentum family has 7 registered trials. Gates were not touched then and are not touched now.

Trial registry: **7 trials before this run → 19 after** (one per family × symbol below).

## Family `trend` — sma200_hysteresis v1.0.0

### trend × SPY — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +150.54% vs buy-and-hold +544.24%, Sharpe 0.55, max drawdown -26.33%, 164 trades, hit rate 62%

#### Gate verdict: **FAIL**

- strategy return: +150.54%
- buy-and-hold return: +544.24%
- null-model p-value: 0.088 (must be ≤ 0.05)
- deflated Sharpe: 0.987 at n_trials=1 (must be ≥ 0.90)
- trial registry id: `0a5d2063-d385-4f8d-9581-f8d5e33f0c7d`

Verbatim gate reasons:
- null-model: p=0.088 > 0.05 (random entries do as well)
- does not beat buy-and-hold (150.5% <= 544.2%)

#### Walk-forward: 4/4 folds positive

- fold returns: +38.41%, +21.29%, +14.76%, +41.12%
- mean return +28.90%, mean Sharpe 0.61, worst fold +14.76%

### trend × QQQ — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +402.30% vs buy-and-hold +1401.27%, Sharpe 0.74, max drawdown -21.64%, 159 trades, hit rate 67%

#### Gate verdict: **FAIL**

- strategy return: +402.30%
- buy-and-hold return: +1401.27%
- null-model p-value: 0.009 (must be ≤ 0.05)
- deflated Sharpe: 0.993 at n_trials=2 (must be ≥ 0.90)
- trial registry id: `356d198e-e7a4-4646-bcca-3d854fd74293`

Verbatim gate reasons:
- does not beat buy-and-hold (402.3% <= 1401.3%)

#### Walk-forward: 4/4 folds positive

- fold returns: +29.80%, +36.76%, +63.79%, +78.69%
- mean return +52.26%, mean Sharpe 0.75, worst fold +29.80%

### trend × MSFT — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +235.78% vs buy-and-hold +1196.66%, Sharpe 0.47, max drawdown -35.34%, 162 trades, hit rate 54%

#### Gate verdict: **FAIL**

- strategy return: +235.78%
- buy-and-hold return: +1196.66%
- null-model p-value: 0.110 (must be ≤ 0.05)
- deflated Sharpe: 0.852 at n_trials=3 (must be ≥ 0.90)
- trial registry id: `2a5c7a88-15ce-4d10-aea8-f2faa05eb035`

Verbatim gate reasons:
- null-model: p=0.110 > 0.05 (random entries do as well)
- does not beat buy-and-hold (235.8% <= 1196.7%)
- deflated Sharpe 0.85 < 0.9 at n_trials=3

#### Walk-forward: 3/4 folds positive

- fold returns: -1.15%, +68.79%, +80.22%, +18.91%
- mean return +41.69%, mean Sharpe 0.47, worst fold -1.15%

### trend × AVGO — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +1336.64% vs buy-and-hold +19476.80%, Sharpe 0.67, max drawdown -61.44%, 183 trades, hit rate 50%

#### Gate verdict: **FAIL**

- strategy return: +1336.64%
- buy-and-hold return: +19476.80%
- null-model p-value: 0.000 (must be ≤ 0.05)
- deflated Sharpe: 0.951 at n_trials=4 (must be ≥ 0.90)
- trial registry id: `40ab6439-ba94-4bb9-af88-8b8ed4f07dcb`

Verbatim gate reasons:
- does not beat buy-and-hold (1336.6% <= 19476.8%)

#### Walk-forward: 4/4 folds positive

- fold returns: +25.18%, +81.21%, +13.14%, +432.84%
- mean return +138.09%, mean Sharpe 0.61, worst fold +13.14%

## Family `meanrev` — connors_rsi2 v1.0.0

### meanrev × SPY — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return -9.74% vs buy-and-hold +544.24%, Sharpe -0.04, max drawdown -26.28%, 121 trades, hit rate 68%

#### Gate verdict: **FAIL**

- strategy return: -9.74%
- buy-and-hold return: +544.24%
- null-model p-value: 0.974 (must be ≤ 0.05)
- deflated Sharpe: 0.438 at n_trials=1 (must be ≥ 0.90)
- trial registry id: `31fa5c82-ac11-4e02-8054-29ca28403352`

Verbatim gate reasons:
- null-model: p=0.974 > 0.05 (random entries do as well)
- does not beat buy-and-hold (-9.7% <= 544.2%)
- deflated Sharpe 0.44 < 0.9 at n_trials=1

#### Walk-forward: 2/4 folds positive

- fold returns: +1.17%, +7.52%, -16.28%, -0.89%
- mean return -2.12%, mean Sharpe -0.01, worst fold -16.28%

### meanrev × QQQ — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +3.24% vs buy-and-hold +1401.27%, Sharpe 0.07, max drawdown -33.29%, 131 trades, hit rate 67%

#### Gate verdict: **FAIL**

- strategy return: +3.24%
- buy-and-hold return: +1401.27%
- null-model p-value: 0.967 (must be ≤ 0.05)
- deflated Sharpe: 0.406 at n_trials=2 (must be ≥ 0.90)
- trial registry id: `231b06f9-5341-4445-b5e1-2c717ce9c03c`

Verbatim gate reasons:
- null-model: p=0.967 > 0.05 (random entries do as well)
- does not beat buy-and-hold (3.2% <= 1401.3%)
- deflated Sharpe 0.41 < 0.9 at n_trials=2

#### Walk-forward: 3/4 folds positive

- fold returns: +2.00%, +15.25%, -26.46%, +19.41%
- mean return +2.55%, mean Sharpe 0.11, worst fold -26.46%

### meanrev × MSFT — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +58.05% vs buy-and-hold +1196.66%, Sharpe 0.29, max drawdown -25.51%, 117 trades, hit rate 69%

#### Gate verdict: **FAIL**

- strategy return: +58.05%
- buy-and-hold return: +1196.66%
- null-model p-value: 0.619 (must be ≤ 0.05)
- deflated Sharpe: 0.620 at n_trials=3 (must be ≥ 0.90)
- trial registry id: `d7d94a62-8659-4593-8b84-01ac891609c4`

Verbatim gate reasons:
- null-model: p=0.619 > 0.05 (random entries do as well)
- does not beat buy-and-hold (58.1% <= 1196.7%)
- deflated Sharpe 0.62 < 0.9 at n_trials=3

#### Walk-forward: 3/4 folds positive

- fold returns: -5.96%, +37.83%, +7.17%, +13.78%
- mean return +13.20%, mean Sharpe 0.32, worst fold -5.96%

### meanrev × AVGO — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +124.80% vs buy-and-hold +19476.80%, Sharpe 0.39, max drawdown -44.00%, 122 trades, hit rate 74%

#### Gate verdict: **FAIL**

- strategy return: +124.80%
- buy-and-hold return: +19476.80%
- null-model p-value: 0.073 (must be ≤ 0.05)
- deflated Sharpe: 0.700 at n_trials=4 (must be ≥ 0.90)
- trial registry id: `ab54b534-189d-49be-b902-8e9a429cff04`

Verbatim gate reasons:
- null-model: p=0.073 > 0.05 (random entries do as well)
- does not beat buy-and-hold (124.8% <= 19476.8%)
- deflated Sharpe 0.70 < 0.9 at n_trials=4

#### Walk-forward: 2/4 folds positive

- fold returns: +3.28%, -1.30%, -1.93%, +124.88%
- mean return +31.23%, mean Sharpe 0.34, worst fold -1.93%

## Family `breakout` — donchian_55_20 v1.0.0

### breakout × SPY — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +20.74% vs buy-and-hold +544.24%, Sharpe 0.18, max drawdown -26.59%, 115 trades, hit rate 58%

#### Gate verdict: **FAIL**

- strategy return: +20.74%
- buy-and-hold return: +544.24%
- null-model p-value: 0.818 (must be ≤ 0.05)
- deflated Sharpe: 0.765 at n_trials=1 (must be ≥ 0.90)
- trial registry id: `5b596b55-347a-4d33-a957-0fb8d8e82241`

Verbatim gate reasons:
- null-model: p=0.818 > 0.05 (random entries do as well)
- does not beat buy-and-hold (20.7% <= 544.2%)
- deflated Sharpe 0.76 < 0.9 at n_trials=1

#### Walk-forward: 4/4 folds positive

- fold returns: +7.27%, +0.18%, +5.87%, +6.14%
- mean return +4.86%, mean Sharpe 0.17, worst fold +0.18%

### breakout × QQQ — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +79.19% vs buy-and-hold +1401.27%, Sharpe 0.38, max drawdown -22.51%, 121 trades, hit rate 56%

#### Gate verdict: **FAIL**

- strategy return: +79.19%
- buy-and-hold return: +1401.27%
- null-model p-value: 0.544 (must be ≤ 0.05)
- deflated Sharpe: 0.841 at n_trials=2 (must be ≥ 0.90)
- trial registry id: `fa31b7b2-dce5-4415-86ee-e70d3616bf2b`

Verbatim gate reasons:
- null-model: p=0.544 > 0.05 (random entries do as well)
- does not beat buy-and-hold (79.2% <= 1401.3%)
- deflated Sharpe 0.84 < 0.9 at n_trials=2

#### Walk-forward: 3/4 folds positive

- fold returns: +10.82%, -0.14%, +9.70%, +47.61%
- mean return +17.00%, mean Sharpe 0.36, worst fold -0.14%

### breakout × MSFT — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +99.54% vs buy-and-hold +1196.66%, Sharpe 0.37, max drawdown -21.20%, 97 trades, hit rate 52%

#### Gate verdict: **FAIL**

- strategy return: +99.54%
- buy-and-hold return: +1196.66%
- null-model p-value: 0.325 (must be ≤ 0.05)
- deflated Sharpe: 0.741 at n_trials=3 (must be ≥ 0.90)
- trial registry id: `4a75283e-b1dd-4ab9-b779-a9eb12117e41`

Verbatim gate reasons:
- null-model: p=0.325 > 0.05 (random entries do as well)
- does not beat buy-and-hold (99.5% <= 1196.7%)
- deflated Sharpe 0.74 < 0.9 at n_trials=3

#### Walk-forward: 4/4 folds positive

- fold returns: +4.64%, +5.91%, +53.34%, +18.05%
- mean return +20.48%, mean Sharpe 0.36, worst fold +4.64%

### breakout × AVGO — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +294.40% vs buy-and-hold +19476.80%, Sharpe 0.48, max drawdown -33.65%, 105 trades, hit rate 50%

#### Gate verdict: **FAIL**

- strategy return: +294.40%
- buy-and-hold return: +19476.80%
- null-model p-value: 0.001 (must be ≤ 0.05)
- deflated Sharpe: 0.812 at n_trials=4 (must be ≥ 0.90)
- trial registry id: `76f83f32-0ecd-488c-a4ce-439939c2385f`

Verbatim gate reasons:
- does not beat buy-and-hold (294.4% <= 19476.8%)
- deflated Sharpe 0.81 < 0.9 at n_trials=4

#### Walk-forward: 4/4 folds positive

- fold returns: +19.97%, +18.18%, +28.86%, +115.87%
- mean return +45.72%, mean Sharpe 0.46, worst fold +18.18%

## Summary

| family | symbol | return | B&H | Sharpe | max DD | trades | null p | DSR (n_trials) | WF folds + | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| trend | SPY | +150.54% | +544.24% | 0.55 | -26.33% | 164 | 0.088 | 0.987 (1) | 4/4 | **FAIL** |
| trend | QQQ | +402.30% | +1401.27% | 0.74 | -21.64% | 159 | 0.009 | 0.993 (2) | 4/4 | **FAIL** |
| trend | MSFT | +235.78% | +1196.66% | 0.47 | -35.34% | 162 | 0.110 | 0.852 (3) | 3/4 | **FAIL** |
| trend | AVGO | +1336.64% | +19476.80% | 0.67 | -61.44% | 183 | 0.000 | 0.951 (4) | 4/4 | **FAIL** |
| meanrev | SPY | -9.74% | +544.24% | -0.04 | -26.28% | 121 | 0.974 | 0.438 (1) | 2/4 | **FAIL** |
| meanrev | QQQ | +3.24% | +1401.27% | 0.07 | -33.29% | 131 | 0.967 | 0.406 (2) | 3/4 | **FAIL** |
| meanrev | MSFT | +58.05% | +1196.66% | 0.29 | -25.51% | 117 | 0.619 | 0.620 (3) | 3/4 | **FAIL** |
| meanrev | AVGO | +124.80% | +19476.80% | 0.39 | -44.00% | 122 | 0.073 | 0.700 (4) | 2/4 | **FAIL** |
| breakout | SPY | +20.74% | +544.24% | 0.18 | -26.59% | 115 | 0.818 | 0.765 (1) | 4/4 | **FAIL** |
| breakout | QQQ | +79.19% | +1401.27% | 0.38 | -22.51% | 121 | 0.544 | 0.841 (2) | 3/4 | **FAIL** |
| breakout | MSFT | +99.54% | +1196.66% | 0.37 | -21.20% | 97 | 0.325 | 0.741 (3) | 4/4 | **FAIL** |
| breakout | AVGO | +294.40% | +19476.80% | 0.48 | -33.65% | 105 | 0.001 | 0.812 (4) | 4/4 | **FAIL** |

## Approval status

**None sought here — by design.** No candidate passed all gates; per the working-style rule these FAIL verdicts are recorded verbatim as deliverables. The gates were not modified, and no strategy row was touched.
