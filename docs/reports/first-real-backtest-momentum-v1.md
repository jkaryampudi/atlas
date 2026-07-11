# First real-data backtest — momentum v1 (SPY, AVGO)

> ## ⚠️ SMALL-SAMPLE WARNING (ADR-0004)
> This evaluation runs on **one year** of history (EODHD plan tier).
> Walk-forward folds and deflated-Sharpe estimates on ~250 sessions are
> **indicative only**. Per ADR-0004 condition 1, these verdicts are **not
> decision-grade**: no approval is sought or recorded, and none may be
> until the full-history re-run.

Gates and evaluation parameters are identical to the committed synthetic-
fixture suite — nothing was tuned for this run (CLAUDE.md: a failing gate
on real data is a valid, reportable result).

- Engine: event-driven, next-open entry, costs 5.0+5.0 bps/side
- Null model: 1000-path random-entry MC, identical exits and costs (ADR-0002 #2)
- Walk-forward: purged+embargoed, k=4, horizon=40, embargo=10, warmup=60 (ADR-0002 #3)
- Every run registered in quant.trial_registry; deflated Sharpe uses the true family trial count (ADR-0002 #1)

## SPY — 2025-07-11 → 2026-07-10 (251 bars, split-adjusted)

Full-window result (after 60-bar warmup): return -3.31%, Sharpe -0.39, max drawdown -7.38%, 6 trades, hit rate 33%

### Gate verdict: **FAIL**

- strategy return: -3.31%
- buy-and-hold return: +12.18%
- null-model p-value: 0.830 (must be ≤ 0.05)
- deflated Sharpe: 0.083 at n_trials=4 (must be ≥ 0.90)
- trial registry id: `314b93f0-8955-44df-9317-33d610dfc3a2`

Verbatim gate reasons:
- null-model: p=0.830 > 0.05 (random entries do as well)
- does not beat buy-and-hold (-3.3% <= 12.2%)
- deflated Sharpe 0.08 < 0.9 at n_trials=4

### Walk-forward: 1/4 folds positive

- fold returns: +3.01%, -1.73%, -2.62%, -1.86%
- mean return -0.80%, mean Sharpe -0.58, worst fold -2.62%

## AVGO — 2025-07-11 → 2026-07-10 (251 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +11.56%, Sharpe 0.62, max drawdown -14.03%, 4 trades, hit rate 50%

### Gate verdict: **FAIL**

- strategy return: +11.56%
- buy-and-hold return: +18.83%
- null-model p-value: 0.059 (must be ≤ 0.05)
- deflated Sharpe: 0.257 at n_trials=5 (must be ≥ 0.90)
- trial registry id: `255c7f32-fad2-4423-ab32-f11e6331308e`

Verbatim gate reasons:
- null-model: p=0.059 > 0.05 (random entries do as well)
- does not beat buy-and-hold (11.6% <= 18.8%)
- deflated Sharpe 0.26 < 0.9 at n_trials=5

### Walk-forward: 2/4 folds positive

- fold returns: +14.28%, -1.62%, +1.12%, -2.57%
- mean return +2.80%, mean Sharpe -0.04, worst fold -2.57%

## Approval status

**None sought.** Per ADR-0004, approval decisions on the 1-year window are
not decision-grade; the strategy row remains untouched. Re-run on full
history after the EODHD plan upgrade before any promotion decision.
