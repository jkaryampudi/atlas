# THE QUALITY TEST — Novy-Marx GP/A on the point-in-time S&P 500 (total)

> ## STRATEGY CANDIDATE #3 THROUGH THE IDENTICAL BAR
> The ONLY difference from the momentum and PEAD runs is the signal: names are
> ranked by gross profitability — trailing-four-quarter gross profit over most-
> recent total assets (Novy-Marx 2013) — not 12-1 momentum or SUE. Universe,
> delisting-aware engine, top-decile equal-weight monthly construction, monkey
> null, deflated Sharpe, purged walk-forward and the binding beat-SPY bar are
> REUSED BY IMPORT from xsmom_pit_run and the committed gauntlet.

> ## NO LOOK-AHEAD IS STRUCTURAL (signals/quality/v1.py)
> filing_date gates when a quarter's figures are knowable; a filing is tradable
> only the NEXT session; a quarter's GP/A is knowable only at the LATEST of its
> four input filings; vendor rows stamped filing_date <= period end (a probed
> defect) are dropped fail-closed at ingestion and never stored; and a filing
> dated after the decision session is physically excluded from the ranking. A
> future quarter's numbers can be flipped wildly and the ranking at t is
> byte-identical (pinned by test).

Pinned spec (textbook, zero search): GP/A = trailing 4 quarters of grossProfit / most recent totalAssets, quarterly statements; all 4 quarters + the newest balance sheet required, else ineligible (missing grossProfit is NEVER derived from revenue minus a cost line); consecutive quarters enforced structurally (period-end span <= 300 days); staleness 252 sessions (an annual cycle without a fresh filing — structural, the paper uses annual data). Winner portfolio is the top decile (max(10, n_eligible // 10)), equal weight, monthly.

- Evaluation window STARTS 2012-07-01 (membership-reliability bound); costs 5.0+5.0 bps/side; null 1000-path monkey MC drawing from the SAME GP/A-eligible set; purged walk-forward k=4, horizon=40, embargo=10; one registered trial per family; deflated Sharpe at the true count.
- Binding benchmark: SPY total return over the same window (ADR-0009); SPY carries no membership row and can never be ranked. Equal-weight-all-eligible shown, NOT binding.
- FINANCIALS: Novy-Marx 2013 EXCLUDES financial firms (structurally low GP/A). This run does NOT exclude them — the full point-in-time universe is ranked and financials simply score what they score. A financials-excluded variant exists behind --exclude-financials as a SECOND registered trial; the orchestrator decides whether to spend it.

## Data quality and honesty — fundamentals coverage

- Panel members with a usable price series: 657
- Members carrying >= 1 stored anchorable quarter: 636 (60476 quarters on record; 52 of them delisted names — survivorship-free)
- Members that ever produce an in-window signal event: 636
- KNOWN COVERAGE COST (fail-closed, not tuned): quarters the vendor stamps with filing_date <= fiscal period end (a physically impossible filing day; e.g. ALL of AVGO 2012-2017) are dropped at ingestion — trusting them would inject weeks of look-ahead. Affected names go signal-less until four consecutive anchorable quarters accumulate; the per-run drop counts are on the ingestion audit event (market.quarterly_fundamentals_ingest.completed).
- Members/eligible at each December rebalance: 2012: 347/313; 2013: 366/337; 2014: 381/351; 2015: 403/372; 2016: 424/407; 2017: 445/425; 2018: 460/440; 2019: 475/459; 2020: 484/468; 2021: 493/480; 2022: 496/483; 2023: 501/488; 2024: 502/484; 2025: 502/494
- Forced delisting liquidations during the run: 39; unfilled buys (died between decision and execution): 0

## Full-window result (start 2012-07-02, panel 2010-01-04 → 2026-07-15, 4157 aligned XNYS sessions, total return (split-adjusted; each dividend reinvested at its ex-date close — market_data/total_return.py))

Return +369.75%, Sharpe 0.73, max drawdown -34.10%, avg turnover 12.97% per rebalance, 168 rebalances

### Gate verdict: **FAIL**

- verdict: **FAIL**
- implication: long-only GP/A does not clear the fund's bar on honest point-in-time membership; the graveyard verdict is recorded verbatim and the factor must not proceed toward approval (a failed gate is a deliverable, not a defect to be tuned away)
- strategy return: +369.75%
- SPY (BINDING benchmark per ADR-0009): +593.76%
- equal-weight all-eligible (informational, NOT binding): +511.14%
- null-model p-value: 0.387 (must be <= 0.05)
- deflated Sharpe: 0.997 at n_trials=1 (must be >= 0.9)
- trial registry id: `2ccc708c-5615-430f-9d7c-c1d32815802a` (family `quality-gpa-tr`)

Verbatim gate reasons:
- null-model: p=0.387 > 0.05 (random same-universe portfolios do as well)
- does not beat SPY buy-and-hold (369.8% <= 593.8%)

### Walk-forward: 4/4 folds positive

- fold returns: +66.27%, +52.75%, +38.44%, +29.37%
- mean return +46.71%, mean Sharpe 0.80, worst fold +29.37%

### Exhibit: verdict vs endpoint (total)

The identical run re-judged at the final date and each of the prior 24 month-ends (exact truncation of the stored curves). A ROBUST edge beats SPY at most endpoints; an edge that beats SPY at only the terminal endpoints is time-concentrated and fragile — read the count below against that standard, not as a guarantee.

- endpoints passing the full gate: 0/25; beating SPY: 0/25

| endpoint | strategy | SPY | null p | DSR | beats SPY | PASS |
|---|---|---|---|---|---|---|
| 2024-07-31 | +328.89% | +394.58% | 0.119 | 0.997 | no | FAIL |
| 2024-08-30 | +341.38% | +406.13% | 0.113 | 0.997 | no | FAIL |
| 2024-09-30 | +357.87% | +416.77% | 0.092 | 0.998 | no | FAIL |
| 2024-10-31 | +342.88% | +412.16% | 0.108 | 0.997 | no | FAIL |
| 2024-11-29 | +369.11% | +442.70% | 0.117 | 0.998 | no | FAIL |
| 2024-12-31 | +348.00% | +429.62% | 0.089 | 0.997 | no | FAIL |
| 2025-01-31 | +363.50% | +443.84% | 0.089 | 0.998 | no | FAIL |
| 2025-02-28 | +360.33% | +436.94% | 0.087 | 0.998 | no | FAIL |
| 2025-03-31 | +332.30% | +407.02% | 0.126 | 0.997 | no | FAIL |
| 2025-04-30 | +329.27% | +402.62% | 0.099 | 0.996 | no | FAIL |
| 2025-05-30 | +343.97% | +434.21% | 0.113 | 0.996 | no | FAIL |
| 2025-06-30 | +352.96% | +461.67% | 0.126 | 0.997 | no | FAIL |
| 2025-07-31 | +359.83% | +474.60% | 0.113 | 0.997 | no | FAIL |
| 2025-08-29 | +373.69% | +486.39% | 0.114 | 0.997 | no | FAIL |
| 2025-09-30 | +375.20% | +507.27% | 0.114 | 0.997 | no | FAIL |
| 2025-10-31 | +360.40% | +521.75% | 0.141 | 0.997 | no | FAIL |
| 2025-11-28 | +374.62% | +522.96% | 0.124 | 0.997 | no | FAIL |
| 2025-12-31 | +375.06% | +523.44% | 0.123 | 0.997 | no | FAIL |
| 2026-01-30 | +378.66% | +532.63% | 0.168 | 0.997 | no | FAIL |
| 2026-02-27 | +381.74% | +527.16% | 0.217 | 0.997 | no | FAIL |
| 2026-03-31 | +340.99% | +496.22% | 0.275 | 0.996 | no | FAIL |
| 2026-04-30 | +344.47% | +558.85% | 0.407 | 0.996 | no | FAIL |
| 2026-05-29 | +358.74% | +593.52% | 0.376 | 0.997 | no | FAIL |
| 2026-06-30 | +365.27% | +586.37% | 0.388 | 0.997 | no | FAIL |
| 2026-07-15 | +369.75% | +593.76% | 0.387 | 0.997 | no | FAIL |

## Summary

| strategy | return | SPY | EW eligible | Sharpe | max DD | turnover | rebalances | null p | DSR (n) | WF + | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| GP/A quality, PIT S&P 500 winner decile | +369.75% | +593.76% | +511.14% | 0.73 | -34.10% | 12.97% | 168 | 0.387 | 0.997 (1) | 4/4 | **FAIL** |

Trial registry: **33 → 34** (one `quality-gpa-tr` trial; family count now 1).

## Annual outcome distribution

No distribution is derived for a failed strategy (house rule: earnings profiles are derived only for validated strategies — profit is a result to be discovered, never an input).

## Approval status

**None sought here — by design.** This is a VALIDATION run on a membership-gated universe of validation-only instruments (is_active=FALSE); it settles whether long-only gross profitability is a real, orthogonal alpha source on honest membership. It does not itself qualify any strategy for the approval workflow. Gates were not modified; no strategy row is touched.


---

## Pre-committed KILL-ONLY trial (start 2016-01-01, family quality-gpa-tr-2016)

# THE QUALITY TEST — Novy-Marx GP/A on the point-in-time S&P 500 (total)

> ## STRATEGY CANDIDATE #3 THROUGH THE IDENTICAL BAR
> The ONLY difference from the momentum and PEAD runs is the signal: names are
> ranked by gross profitability — trailing-four-quarter gross profit over most-
> recent total assets (Novy-Marx 2013) — not 12-1 momentum or SUE. Universe,
> delisting-aware engine, top-decile equal-weight monthly construction, monkey
> null, deflated Sharpe, purged walk-forward and the binding beat-SPY bar are
> REUSED BY IMPORT from xsmom_pit_run and the committed gauntlet.

> ## NO LOOK-AHEAD IS STRUCTURAL (signals/quality/v1.py)
> filing_date gates when a quarter's figures are knowable; a filing is tradable
> only the NEXT session; a quarter's GP/A is knowable only at the LATEST of its
> four input filings; vendor rows stamped filing_date <= period end (a probed
> defect) are dropped fail-closed at ingestion and never stored; and a filing
> dated after the decision session is physically excluded from the ranking. A
> future quarter's numbers can be flipped wildly and the ranking at t is
> byte-identical (pinned by test).

Pinned spec (textbook, zero search): GP/A = trailing 4 quarters of grossProfit / most recent totalAssets, quarterly statements; all 4 quarters + the newest balance sheet required, else ineligible (missing grossProfit is NEVER derived from revenue minus a cost line); consecutive quarters enforced structurally (period-end span <= 300 days); staleness 252 sessions (an annual cycle without a fresh filing — structural, the paper uses annual data). Winner portfolio is the top decile (max(10, n_eligible // 10)), equal weight, monthly.

- Evaluation window STARTS 2012-07-01 (membership-reliability bound); costs 5.0+5.0 bps/side; null 1000-path monkey MC drawing from the SAME GP/A-eligible set; purged walk-forward k=4, horizon=40, embargo=10; one registered trial per family; deflated Sharpe at the true count.
- Binding benchmark: SPY total return over the same window (ADR-0009); SPY carries no membership row and can never be ranked. Equal-weight-all-eligible shown, NOT binding.
- FINANCIALS: Novy-Marx 2013 EXCLUDES financial firms (structurally low GP/A). This run does NOT exclude them — the full point-in-time universe is ranked and financials simply score what they score. A financials-excluded variant exists behind --exclude-financials as a SECOND registered trial; the orchestrator decides whether to spend it.

## Data quality and honesty — fundamentals coverage

- Panel members with a usable price series: 657
- Members carrying >= 1 stored anchorable quarter: 636 (60476 quarters on record; 52 of them delisted names — survivorship-free)
- Members that ever produce an in-window signal event: 636
- KNOWN COVERAGE COST (fail-closed, not tuned): quarters the vendor stamps with filing_date <= fiscal period end (a physically impossible filing day; e.g. ALL of AVGO 2012-2017) are dropped at ingestion — trusting them would inject weeks of look-ahead. Affected names go signal-less until four consecutive anchorable quarters accumulate; the per-run drop counts are on the ingestion audit event (market.quarterly_fundamentals_ingest.completed).
- Members/eligible at each December rebalance: 2016: 424/407; 2017: 445/425; 2018: 460/440; 2019: 475/459; 2020: 484/468; 2021: 493/480; 2022: 496/483; 2023: 501/488; 2024: 502/484; 2025: 502/494
- Forced delisting liquidations during the run: 39; unfilled buys (died between decision and execution): 0

## Full-window result (start 2016-01-04, panel 2010-01-04 → 2026-07-15, 4157 aligned XNYS sessions, total return (split-adjusted; each dividend reinvested at its ex-date close — market_data/total_return.py))

Return +188.87%, Sharpe 0.65, max drawdown -34.10%, avg turnover 13.40% per rebalance, 126 rebalances

### Gate verdict: **FAIL**

- verdict: **FAIL**
- implication: long-only GP/A does not clear the fund's bar on honest point-in-time membership; the graveyard verdict is recorded verbatim and the factor must not proceed toward approval (a failed gate is a deliverable, not a defect to be tuned away)
- strategy return: +188.87%
- SPY (BINDING benchmark per ADR-0009): +364.89%
- equal-weight all-eligible (informational, NOT binding): +286.49%
- null-model p-value: 0.716 (must be <= 0.05)
- deflated Sharpe: 0.982 at n_trials=1 (must be >= 0.9)
- trial registry id: `76eef0d1-3cd8-43f0-931d-b9d3ca3be902` (family `quality-gpa-tr-2016`)

Verbatim gate reasons:
- null-model: p=0.716 > 0.05 (random same-universe portfolios do as well)
- does not beat SPY buy-and-hold (188.9% <= 364.9%)

### Walk-forward: 4/4 folds positive

- fold returns: +44.09%, +37.63%, +3.02%, +28.07%
- mean return +28.20%, mean Sharpe 0.67, worst fold +3.02%

## Summary

| strategy | return | SPY | EW eligible | Sharpe | max DD | turnover | rebalances | null p | DSR (n) | WF + | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| GP/A quality, PIT S&P 500 winner decile | +188.87% | +364.89% | +286.49% | 0.65 | -34.10% | 13.40% | 126 | 0.716 | 0.982 (1) | 4/4 | **FAIL** |

Trial registry: **34 → 35** (one `quality-gpa-tr-2016` trial; family count now 1).

## Annual outcome distribution

No distribution is derived for a failed strategy (house rule: earnings profiles are derived only for validated strategies — profit is a result to be discovered, never an input).

## Approval status

**None sought here — by design.** This is a VALIDATION run on a membership-gated universe of validation-only instruments (is_active=FALSE); it settles whether long-only gross profitability is a real, orthogonal alpha source on honest membership. It does not itself qualify any strategy for the approval workflow. Gates were not modified; no strategy row is touched.
