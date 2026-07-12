# Decision-grade backtest — momentum v1 (SPY, AVGO)

> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)
> This evaluation runs on the **full vendor history** (2010-01-04 → 2026-07-10).
> Verdicts below are decision-grade: an approval decision MAY rest on
> them — pass or fail, recorded verbatim.

Gates and evaluation parameters are identical to the committed synthetic-
fixture suite — nothing was tuned for this run (CLAUDE.md: a failing gate
on real data is a valid, reportable result).

- Engine: event-driven, next-open entry, costs 5.0+5.0 bps/side
- Null model: 1000-path random-entry MC, identical exits and costs (ADR-0002 #2)
- Walk-forward: purged+embargoed, k=4, horizon=40, embargo=10, warmup=60 (ADR-0002 #3)
- Every run registered in quant.trial_registry; deflated Sharpe uses the true family trial count (ADR-0002 #1)

## SPY — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +7.77%, Sharpe 0.10, max drawdown -27.66%, 131 trades, hit rate 40%

### Gate verdict: **FAIL**

- strategy return: +7.77%
- buy-and-hold return: +544.24%
- null-model p-value: 0.923 (must be ≤ 0.05)
- deflated Sharpe: 0.182 at n_trials=6 (must be ≥ 0.90)
- trial registry id: `c86cf7c9-0070-4343-aea1-c1bc13f5e862`

Verbatim gate reasons:
- null-model: p=0.923 > 0.05 (random entries do as well)
- does not beat buy-and-hold (7.8% <= 544.2%)
- deflated Sharpe 0.18 < 0.9 at n_trials=6

### Walk-forward: 2/4 folds positive

- fold returns: -2.36%, -7.73%, +12.45%, +6.38%
- mean return +2.18%, mean Sharpe 0.07, worst fold -7.73%

## AVGO — 2010-01-04 → 2026-07-10 (4154 bars, split-adjusted)

Full-window result (after 60-bar warmup): return +106.45%, Sharpe 0.31, max drawdown -62.87%, 138 trades, hit rate 43%

### Gate verdict: **FAIL**

- strategy return: +106.45%
- buy-and-hold return: +19476.80%
- null-model p-value: 0.126 (must be ≤ 0.05)
- deflated Sharpe: 0.443 at n_trials=7 (must be ≥ 0.90)
- trial registry id: `a294c30d-b28e-48e6-a728-4e1293f182f9`

Verbatim gate reasons:
- null-model: p=0.126 > 0.05 (random entries do as well)
- does not beat buy-and-hold (106.5% <= 19476.8%)
- deflated Sharpe 0.44 < 0.9 at n_trials=7

### Walk-forward: 2/4 folds positive

- fold returns: -41.54%, -19.17%, +74.59%, +102.73%
- mean return +29.15%, mean Sharpe 0.23, worst fold -41.54%

## Approval status

**None sought here.** These decision-grade verdicts feed the separate
approval workflow (dcp/backtest/approval.py) — the strategy row is
only ever moved by that gate, never by a report.
history after the EODHD plan upgrade before any promotion decision.
