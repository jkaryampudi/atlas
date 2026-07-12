# Cross-sectional momentum — xsmom v1 (12-1, top 10) over the ADR-0007 universe (2026-07)

> ## ⚠️ SURVIVORSHIP BIAS CAVEAT — read before the verdict
> The universe is **TODAY'S S&P 100 snapshot** (ADR-0007, pinned at
> adoption): every name in it survived and succeeded into 2026. A
> 2010→2026 backtest on it therefore carries **index-membership
> survivorship bias**, which **INFLATES cross-sectional momentum
> results** (past winners that later collapsed out of the index are
> missing from the loser side of every ranking), and **deflated
> Sharpe does NOT correct for it** — DSR deflates for multiple
> testing, not for a biased universe. Any PASS below is therefore
> **"PASS pending point-in-time constituent validation"**. Fetching
> historical constituents is deliberately out of scope for this run.

> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)
> Full vendor history (2010-01-04 → 2026-07-10); verdicts are
> decision-grade subject to the survivorship caveat above — pass or fail,
> recorded verbatim.

Textbook parameters (Jegadeesh & Titman 1993, cited in the module
docstring), chosen without any parameter search; ONE registered trial
for this run (family `xsmom`). Gate thresholds are IMPORTED from the
committed validation module — nothing restated, nothing tuned
(CLAUDE.md: a failing gate on real data is a valid, reportable result).

- Engine: portfolio target-weight, monthly rebalance at month-end close, execution at next session's open, costs 5.0+5.0 bps/side on turnover
- Null model: 1000-path monkey MC — at each rebalance, 10 names drawn uniformly from the SAME eligible set, identical engine/costs (ADR-0002 #2)
- Walk-forward: purged+embargoed on the daily timeline, k=4, horizon=40, embargo=10 (constants from real_run), warmup=252 (xsmom seasoning replaces the single-series indicator warmup of 60 — documented in xsmom_run) (ADR-0002 #3)
- Registered in quant.trial_registry; deflated Sharpe uses the true family trial count (ADR-0002 #1)

## Universe and data honesty

- Panel: 110 symbols included, 2010-01-04 → 2026-07-10 (4154 aligned XNYS sessions, split-adjusted)
- Late listings join point-in-time once seasoned (252 prior sessions); they are never backfilled into earlier rankings
- Excluded: 2 symbol(s) — per-instrument completeness substitute for assert_symbol_data_clean (documented in xsmom_run):
  - INDA: 25 missing session(s) between its inception 2012-02-03 and end 2026-07-10 (first: 2012-05-07)
  - NDIA: non-US session calendar (market=AU) — cannot align to a US close matrix

## Full-window result (start 2011-01-03, after 252-session seasoning)

Return +4584.20%, Sharpe 1.09, max drawdown -37.51%, avg turnover 57.65% per rebalance (sum |Δw|, both sides), 186 rebalances

### Gate verdict: **PASS**

- verdict: **PASS — pending point-in-time constituent validation** (see caveat above)
- strategy return: +4584.20%
- SPY buy-and-hold (BINDING benchmark — the fund's actual alternative): +482.57%
- equal-weight all-eligible, monthly (informational, shown per protocol, NOT binding): +790.53%
- null-model p-value: 0.000 (must be ≤ 0.05)
- deflated Sharpe: 1.000 at n_trials=1 (must be ≥ 0.9)
- trial registry id: `ec26ab76-6b1a-4d02-a03f-a9c3c6bf89a8`

### Walk-forward: 4/4 folds positive

- fold returns: +138.30%, +143.55%, +102.16%, +388.91%
- mean return +193.23%, mean Sharpe 1.18, worst fold +102.16%

## Summary

| strategy | return | SPY B&H | EW eligible | Sharpe | max DD | avg turnover | rebalances | null p | DSR (n_trials) | WF folds + | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| xsmom v1 | +4584.20% | +482.57% | +790.53% | 1.09 | -37.51% | 57.65% | 186 | 0.000 | 1.000 (1) | 4/4 | **PASS** |

Trial registry: **19 trials before this run → 20 after** (ONE xsmom trial; family count now 1).

## Approval status

**None sought here — by design.** The verdict is PASS pending point-in-time constituent validation: the survivorship caveat above must be resolved (historical index membership) before this result may enter the approval workflow (dcp/backtest/approval.py). The gates were not modified; the strategy row is untouched.
