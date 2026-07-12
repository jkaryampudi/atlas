# THE DEFINITIVE MOMENTUM TEST — xsmom recipe on the point-in-time S&P 500, dead companies included (2026-07)

> ## WHY THIS IS THE DEFINITIVE TEST
> Membership is POINT-IN-TIME: at every rebalance the ranked universe is the
> S&P 500 as it stood THAT DAY (validation.index_membership, vendor's
> HistoricalTickerComponents), INCLUDING companies that later collapsed, were
> acquired, or were delisted — their price series are in the panel and a held
> name that dies mid-hold is liquidated at its final available close. This
> removes the index-membership survivorship bias that made the S&P-100 result
> conditional (docs/reports/xsmom-momentum-2026-07.md) and that the sector-ETF
> cross-check (docs/reports/xsmom-etf-crosscheck-2026-07.md) could only probe
> on nine fixed funds. It settles the survivorship question for this recipe.

> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)
> Evaluation window 2012-07-02 → 2026-07-10 (>= 10 years); the verdict is
> decision-grade FOR THE SURVIVORSHIP QUESTION — pass or fail, recorded verbatim.

Same textbook recipe (Jegadeesh & Titman 1993, 12-1, monthly, equal weight,
252-session seasoning), zero parameter sweeps. Winner portfolio is the
TOP DECILE of the point-in-time eligible set (n_eligible // 10, floored at
v1's 10) — the J&T construction is fractional, and the eligible set now
varies month to month. ONE registered trial (family `xsmom-pit`). Gate
thresholds are IMPORTED from the committed validation module — nothing restated,
nothing tuned.

- Evaluation window STARTS 2012-07-01: vendor EndDates are sparse before ~2012
  (prong-B probe), so earlier membership is unreliable — documented fail-closed bound
- Engine: portfolio target-weight, monthly rebalance at month-end close, execution at next session's open, costs 5.0+5.0 bps/side on turnover
- DELISTING RULE (hand-pinned by test): a held name whose series ends mid-hold is
  liquidated at its final available close, pays the same per-side cost, and the
  proceeds sit in cash until the next rebalance; a pending buy whose name dies
  between decision and execution does not fill
- Null model: 1000-path monkey MC — at each rebalance, the SAME COUNT of names
  drawn uniformly from the SAME point-in-time eligible set, identical engine/costs/
  delisting rule (ADR-0002 #2)
- Walk-forward: purged+embargoed on the daily timeline, k=4, horizon=40, embargo=10 (constants from real_run), warmup = the evaluation-window start index (dominates 252-session seasoning and keeps every fold past 2012-07-01) (ADR-0002 #3)
- Registered in quant.trial_registry; deflated Sharpe uses the true family trial count (ADR-0002 #1)
- Benchmark: SPY buy-and-hold over the same window — the BINDING comparison per ADR-0009; SPY carries no membership row and can never be ranked; equal-weight-all-eligible shown per protocol, NOT binding
- Convention note (inherited from the round-2 machinery, applied identically to strategy, null and both benchmarks): bars are split-adjusted PRICE returns — dividends are not reinvested on either side of the comparison

## Data quality and honesty

### Membership reconstruction (fail-closed rule, market_data/index_membership.py)

- Vendor rows: 817 total; usable 674; EXCLUDED fail-closed: 96 null-StartDate+delisted, 47 null-StartDate+departed (unknowable intervals; several demonstrably carry ticker-reuse confusion)
- Usable members intersecting the window: 674
- ⚠️ RECONSTRUCTION UNDERCOUNT: at the first rebalance (2012-07-31) the reconstructed membership is 339 names (true S&P 500 ≈ 500) because every null-StartDate row was excluded fail-closed — and those missing rows are ALL departures (names that later left the index). The early-window eligible set is therefore still survivor-tilted at the margin; this bias is one-directional (it FLATTERS momentum, as the S&P-100 run showed) and shrinks to zero by 2026-06-30 (502 members).
- Members/eligible at each December rebalance: 2012: 347/342; 2013: 366/360; 2014: 381/376; 2015: 403/395; 2016: 424/420; 2017: 445/443; 2018: 460/457; 2019: 475/468; 2020: 484/477; 2021: 493/486; 2022: 496/489; 2023: 501/495; 2024: 502/495; 2025: 502/496

### Price coverage (per-instrument completeness, fail closed per series)

- 657 of 674 window members have usable series in the panel (587 living, 70 delisted)
- Missing series (no stored vendor bars): 3 (3 delisted)
- Excluded by completeness rules: 14 (3 delisted)
- DELISTED-member price coverage: 70 of 76 = 92%
- Forced delisting liquidations during the run: 7; unfilled buys (died between decision and execution): 1

Excluded series (first 30):
  - ABMD [delisted]: 1 missing session(s) between its inception 2010-07-01 and end 2022-12-23 (first: 2012-12-06)
  - CZR: 1 missing session(s) between its inception 2010-07-01 and end 2026-07-10 (first: 2014-09-19)
  - FRC [delisted]: 10 missing session(s) between its inception 2010-12-09 and end 2023-05-02 (first: 2015-12-17)
  - FTV: 3 missing session(s) between its inception 2016-06-13 and end 2026-07-10 (first: 2016-06-16)
  - KTB: 1 missing session(s) between its inception 2019-05-09 and end 2026-07-10 (first: 2019-05-13)
  - NKTR: 2 missing session(s) between its inception 2010-07-01 and end 2026-07-10 (first: 2014-06-11)
  - ONL: 5 missing session(s) between its inception 2021-11-01 and end 2026-07-10 (first: 2021-11-02)
  - Q: 204 missing session(s) between its inception 2024-12-31 and end 2026-07-10 (first: 2025-01-02)
  - SATS: 4 missing session(s) between its inception 2010-07-01 and end 2026-07-10 (first: 2026-07-06)
  - SBNY: 10 missing session(s) between its inception 2010-07-01 and end 2026-07-10 (first: 2023-03-14)
  - SOLS: 199 missing session(s) between its inception 2024-12-31 and end 2026-07-10 (first: 2025-01-02)
  - SW: 1 bar(s) on non-session dates (first: 2025-11-27)
  - VICI: 3 missing session(s) between its inception 2017-10-18 and end 2026-07-10 (first: 2017-10-30)
  - ZIMV [delisted]: 8 missing session(s) between its inception 2022-02-16 and end 2025-11-03 (first: 2025-10-22)

Missing series (3): IILGV*, LEN-N*, MRP_old*  (* = delisted)

## Full-window result (start 2012-07-02, panel 2010-01-04 → 2026-07-10, 4154 aligned XNYS sessions, split-adjusted)

Return +596.92%, Sharpe 0.76, max drawdown -36.71%, avg turnover 62.87% per rebalance (sum |Δw|, both sides), 168 rebalances

### Gate verdict: **PASS**

- verdict: **PASS**
- implication for the S&P-100 → ETF results chain: cross-sectional momentum on the point-in-time S&P 500 — dead companies included — clears the full gauntlet: the effect is real AND beats the fund's actual alternative; the S&P-100 (+4,584%) magnitude stays inflated by survivorship, but the strategy family is validated on honest membership
- strategy return: +596.92%
- SPY buy-and-hold (BINDING benchmark per ADR-0009 — the fund's actual alternative): +443.76%
- equal-weight all-eligible, monthly (informational, shown per protocol, NOT binding): +363.22%
- null-model p-value: 0.000 (must be ≤ 0.05)
- deflated Sharpe: 0.998 at n_trials=1 (must be ≥ 0.9)
- trial registry id: `446ad240-9636-4f1e-a56e-9bd47477d658`

### Walk-forward: 4/4 folds positive

- fold returns: +76.39%, +43.89%, +35.83%, +112.64%
- mean return +67.19%, mean Sharpe 0.83, worst fold +35.83%

## Summary

| strategy | return | SPY B&H | EW eligible | Sharpe | max DD | avg turnover | rebalances | null p | DSR (n_trials) | WF folds + | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| xsmom recipe, PIT S&P 500 winner decile | +596.92% | +443.76% | +363.22% | 0.76 | -36.71% | 62.87% | 168 | 0.000 | 0.998 (1) | 4/4 | **PASS** |

Implication: cross-sectional momentum on the point-in-time S&P 500 — dead companies included — clears the full gauntlet: the effect is real AND beats the fund's actual alternative; the S&P-100 (+4,584%) magnitude stays inflated by survivorship, but the strategy family is validated on honest membership.

Trial registry: **24 trials before this run → 25 after** (ONE xsmom-pit trial; family count now 1).

## Annual outcome distribution

> **History is not a forecast.** This is the DISPERSION a strategy like this has
> exhibited — any single future year can land anywhere in (or outside) this range;
> the median is not a promise.

Per-calendar-year returns (identical engine, window and costs for both columns; partial years noted):

| year | strategy | SPY B&H | note |
|---|---|---|---|
| 2012 | +9.01% | +2.57% | partial (from 2012-07-02) |
| 2013 | +40.45% | +29.69% |  |
| 2014 | +9.51% | +11.29% |  |
| 2015 | +5.21% | -0.81% |  |
| 2016 | +1.96% | +9.64% |  |
| 2017 | +17.22% | +19.38% |  |
| 2018 | -6.57% | -6.35% |  |
| 2019 | +23.19% | +28.79% |  |
| 2020 | +18.49% | +16.16% |  |
| 2021 | +14.85% | +27.04% |  |
| 2022 | -2.52% | -19.48% |  |
| 2023 | +10.15% | +24.29% |  |
| 2024 | +24.82% | +23.30% |  |
| 2025 | +14.20% | +16.35% |  |
| 2026 | +37.90% | +10.71% | partial (through 2026-07-10) |

Block bootstrap of annual outcomes: daily returns resampled in 21-session blocks, 1000 seeded draws of 252 sessions (seed 7). The rng stream depends only on (seed, series length), so strategy and SPY draw identical block positions — paired draws, same method for both columns.

| percentile of simulated annual return | strategy | SPY B&H |
|---|---|---|
| 10th | -7.50% | -6.08% |
| 25th | +2.36% | +3.44% |
| median | +15.14% | +13.46% |
| 75th | +28.40% | +23.79% |
| 90th | +43.29% | +34.82% |

## Approval status

**None sought here — by design.** This is a VALIDATION run on a membership-gated universe built from validation-only instruments (is_active=FALSE): it settles the survivorship question for the xsmom family; it does not itself qualify any strategy for the approval workflow (dcp/backtest/approval.py). The gates were not modified; no strategy row is touched.
