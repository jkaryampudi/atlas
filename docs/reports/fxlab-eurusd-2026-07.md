# fxlab gauntlet — EUR/USD daily, three textbook candidates (2026-07)

> ## ADR-0008: the benchmark is ZERO and no profit target exists
> There is nothing to hold in FX: a candidate must beat doing
> nothing, after honest costs, through the full gauntlet. **No profit
> target exists anywhere in the sandbox** — the Principal's original
> "self-learn until it generates A$50/day" framing was REFUSED and is
> recorded in ADR-0008: a learning loop with a profit quota as its
> stopping rule converges on memorized noise. If something passes,
> its earnings profile (expectancy, Sharpe, drawdown, P&L dispersion)
> is a DERIVED output reported afterward — whatever the numbers are.
> **The expected outcome of this report is failure**; verdicts are
> recorded verbatim.

Window: 2010-01-01..2026-07-10 (4311 vendor bars, EODHD; volume and weekend stubs
discarded as vendor artifacts). Warmup 200 bars (longest candidate lookback).

- Engine: daily long/short in {-1, 0, +1}, decided on close of t,
  executed at open of t+1; final open position force-liquidated at the
  last close (atlas/fxlab/engine.py)
- Honest costs (ADR-0008 §4, conservative placeholders, ADR-0003 Tier-1
  recalibratable): spread 0.00008 per position-change leg
  (a round trip pays 0.00016 ~ 1.6 pips), swap
  0.00003 per night held, either direction
- Null model: 1000 seeded paths (seed 7) of the candidate's own
  position blocks order-shuffled — matched exposure, turnover matched
  from above — through the SAME engine and costs
- Thresholds read from dcp/backtest/validation.py, never restated:
  null p <= 0.05, deflated Sharpe >= 0.9 at the true count of
  ALL fxlab- trials in quant.trial_registry (same registry as the fund)
- Purged walk-forward: k=4, horizon=40, embargo=10
  (imported from dcp/backtest/real_run.py); clearing = the approval
  gate's majority rule (positive folds >= k//2 + 1)

Trial registry: **0 fxlab trials before this run -> 3 after**; deflated Sharpe below uses n_trials=3.

## ma_cross

Full-window result (after 200-bar warmup): return -19.52% vs benchmark 0.00%, Sharpe -0.11, max drawdown -32.39%, 27 trades; exposure long 48% / short 52% / flat 0%

### Gate verdict: **FAIL**

- strategy return after costs: -19.52% (must be > 0%)
- null-model p-value: 0.512 (must be <= 0.05)
- deflated Sharpe: 0.100 at n_trials=3 (must be >= 0.9)
- trial registry id: `b3495f33-4dce-4110-8a7d-df7ece797dd4`

Verbatim gate reasons:
- does not beat doing nothing: -19.52% <= 0% after costs (ADR-0008 §5: the benchmark is zero)
- null-model: p=0.512 > 0.05 (random long/short blocks do as well)
- deflated Sharpe 0.10 < 0.9 at n_trials=3
- walk-forward: only 1/4 folds positive

### Walk-forward: 1/4 folds positive

- fold returns: -7.97%, -1.15%, +15.50%, -23.55%
- mean fold return -4.29%, worst fold -23.55%

## donchian

Full-window result (after 200-bar warmup): return -19.95% vs benchmark 0.00%, Sharpe -0.14, max drawdown -31.91%, 135 trades; exposure long 33% / short 41% / flat 26%

### Gate verdict: **FAIL**

- strategy return after costs: -19.95% (must be > 0%)
- null-model p-value: 0.599 (must be <= 0.05)
- deflated Sharpe: 0.079 at n_trials=3 (must be >= 0.9)
- trial registry id: `3d6058e1-954b-4d07-b733-f35739cc4be8`

Verbatim gate reasons:
- does not beat doing nothing: -19.95% <= 0% after costs (ADR-0008 §5: the benchmark is zero)
- null-model: p=0.599 > 0.05 (random long/short blocks do as well)
- deflated Sharpe 0.08 < 0.9 at n_trials=3
- walk-forward: only 2/4 folds positive

### Walk-forward: 2/4 folds positive

- fold returns: -6.05%, +4.14%, -18.38%, +0.04%
- mean fold return -5.06%, worst fold -18.38%

## rsi_fade

Full-window result (after 200-bar warmup): return +7.60% vs benchmark 0.00%, Sharpe 0.12, max drawdown -16.49%, 48 trades; exposure long 15% / short 10% / flat 74%

### Gate verdict: **FAIL**

- strategy return after costs: +7.60% (must be > 0%)
- null-model p-value: 0.195 (must be <= 0.05)
- deflated Sharpe: 0.350 at n_trials=3 (must be >= 0.9)
- trial registry id: `4e668fcf-7da4-49de-9949-dea088c73251`

Verbatim gate reasons:
- null-model: p=0.195 > 0.05 (random long/short blocks do as well)
- deflated Sharpe 0.35 < 0.9 at n_trials=3
- walk-forward: only 2/4 folds positive

### Walk-forward: 2/4 folds positive

- fold returns: +5.72%, -7.63%, -1.07%, +11.48%
- mean fold return +2.12%, worst fold -7.63%

## Verdict table

| candidate | return | Sharpe | max DD | trades | long/short/flat | null p | DSR (n_trials) | WF folds + | verdict |
|---|---|---|---|---|---|---|---|---|---|
| ma_cross | -19.52% | -0.11 | -32.39% | 27 | 48%/52%/0% | 0.512 | 0.100 (3) | 1/4 | **FAIL** |
| donchian | -19.95% | -0.14 | -31.91% | 135 | 33%/41%/26% | 0.599 | 0.079 (3) | 2/4 | **FAIL** |
| rsi_fade | +7.60% | 0.12 | -16.49% | 48 | 15%/10%/74% | 0.195 | 0.350 (3) | 2/4 | **FAIL** |

## Earnings profile (ADR-0008 §7)

**REFUSED — nothing passed.** Earnings profiles are DERIVED outputs
of a candidate that has survived the gauntlet (ADR-0008 §7); no
candidate did, so there is no earnings profile to report and none
will be projected, extrapolated or targeted. Profit is a result to
be discovered, never an input parameter.

## Status

Research-only, forever, under ADR-0008: no live trading, no paper
ledger shared with the equity book, no path to the risk engine, bridge,
desk or approval queue. Promotion out of the sandbox would require a
new, separate signed ADR. Gates were not modified for this run; FAIL
verdicts above are deliverables, recorded verbatim.
