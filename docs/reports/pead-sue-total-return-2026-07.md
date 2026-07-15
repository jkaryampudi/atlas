# THE EARNINGS-SURPRISE TEST — SUE/PEAD on the point-in-time S&P 500 (total)

> ## THE ONE ORTHOGONAL FACTOR THROUGH THE IDENTICAL BAR
> The ONLY difference from the momentum run is the signal: names are ranked by
> Standardized Unexpected Earnings (Foster-Olsen-Shevlin SUE), not 12-1 price
> momentum. Universe, delisting-aware engine, top-decile equal-weight monthly
> construction, monkey null, deflated Sharpe, purged walk-forward and the binding
> beat-SPY bar are REUSED BY IMPORT from xsmom_pit_run and the committed gauntlet.

> ## NO LOOK-AHEAD IS STRUCTURAL (signals/pead/v1.py)
> The report_date gates when a surprise is knowable; an after-market print is
> tradable only the NEXT session; the standardization of a report uses ONLY
> strictly-prior reports; and a report dated after the decision session is
> physically excluded from the ranking. A future report's numbers can be flipped
> wildly and the ranking at t is byte-identical (pinned by test).

Pinned spec (textbook, zero search): SUE = (epsActual - epsEstimate) / stdev(surprise over the prior 8 reported quarters); >= 4 priors required (else ineligible); drift-capture staleness window 63 sessions; EPS is vendor backward-split-adjusted to the current basis and used directly (no on-read adjustment). Winner portfolio is the top decile (max(10, n_eligible // 10)), equal weight, monthly.

- Evaluation window STARTS 2012-07-01 (membership-reliability bound); costs 5.0+5.0 bps/side; null 1000-path monkey MC drawing from the SAME PEAD-eligible set; purged walk-forward k=4, horizon=40, embargo=10; one registered trial per family; deflated Sharpe at the true count.
- Binding benchmark: SPY total return over the same window (ADR-0009); SPY carries no membership row and can never be ranked. Equal-weight-all-eligible shown, NOT binding.

## Data quality and honesty — earnings coverage

- Panel members with a usable price series: 657
- Members carrying >= 1 stored surprise: 637 (60109 completed reports on record; 53 of them delisted names — survivorship-free)
- Members that ever produce a standardizable SUE in-window: 637
- Members/eligible at each December rebalance: 2012: 347/326; 2013: 366/343; 2014: 381/361; 2015: 403/382; 2016: 424/410; 2017: 445/439; 2018: 460/453; 2019: 475/470; 2020: 484/474; 2021: 493/482; 2022: 496/488; 2023: 501/496; 2024: 502/494; 2025: 502/497
- Forced delisting liquidations during the run: 38; unfilled buys (died between decision and execution): 0

## Full-window result (start 2012-07-02, panel 2010-01-04 → 2026-07-14, 4156 aligned XNYS sessions, total return (split-adjusted; each dividend reinvested at its ex-date close — market_data/total_return.py))

Return +616.75%, Sharpe 0.87, max drawdown -41.17%, avg turnover 64.81% per rebalance, 168 rebalances

### Gate verdict: **PASS** (full-window gate only — see the caveat)

> **POST-AUDIT VERDICT CAVEAT (2026-07-15).** The numbers here are verbatim from
> the corrected run; three prose statements were fixed after an adversarial
> re-audit (stale "split-adjusted on read"; the false "robust edge survives the
> choice of endpoint" caption; and this caveat added). The full-window gate
> PASSES, but a full-window PASS is NECESSARY, NOT SUFFICIENT:
> - the **pre-committed 2016 kill-only trial FAILS** (+362.92% vs SPY +363.05%,
>   below — by the demote-only protocol this is a strike momentum did not incur);
> - the edge beats SPY at only **4 of 25** endpoints, all in the final months
>   (at 2026-03-31 it was still trailing);
> - it is driven by a single **AI/semiconductor cluster** (MU, AMD, AMAT, …) that
>   OVERLAPS momentum's winners — so it is NOT the orthogonal diversifier sought.
> This is a Principal decision, not a clean validation.

- verdict: **PASS** on the binding full-window beat-SPY gate ONLY
- implication: earnings-surprise (SUE/PEAD) clears the binding full-window
  beat-SPY gate on honest point-in-time membership. This is the gate result
  only; robustness (the failed kill trial, the 4/25 endpoint concentration, and
  the lack of orthogonality to momentum) must be weighed before any approval.
- strategy return: +616.75%
- SPY (BINDING benchmark per ADR-0009): +591.02%
- equal-weight all-eligible (informational, NOT binding): +514.59%
- null-model p-value: 0.000 (must be <= 0.05)
- deflated Sharpe: 0.997 at n_trials=2 (must be >= 0.9)
- trial registry id: `51c72148-7be4-4baa-a7d2-66b1161a55bb` (family `pead-sue-tr`)

### Walk-forward: 4/4 folds positive

- fold returns: +64.95%, +71.67%, +48.42%, +78.69%
- mean return +65.93%, mean Sharpe 1.02, worst fold +48.42%

### Exhibit: verdict vs endpoint (total)

The identical run re-judged at the final date and each of the prior 24 month-ends (exact truncation of the stored curves). A robust edge beats SPY at MOST endpoints; this one beats SPY at only the terminal endpoints (see the count) — time-concentrated and fragile.

- endpoints passing the full gate: 4/25; beating SPY: 4/25

| endpoint | strategy | SPY | null p | DSR | beats SPY | PASS |
|---|---|---|---|---|---|---|
| 2024-07-31 | +360.42% | +394.58% | 0.048 | 0.987 | no | FAIL |
| 2024-08-30 | +377.06% | +406.13% | 0.040 | 0.989 | no | FAIL |
| 2024-09-30 | +383.95% | +416.77% | 0.045 | 0.989 | no | FAIL |
| 2024-10-31 | +373.57% | +412.16% | 0.047 | 0.988 | no | FAIL |
| 2024-11-29 | +400.83% | +442.70% | 0.048 | 0.991 | no | FAIL |
| 2024-12-31 | +380.53% | +429.62% | 0.035 | 0.989 | no | FAIL |
| 2025-01-31 | +396.91% | +443.84% | 0.032 | 0.990 | no | FAIL |
| 2025-02-28 | +400.19% | +436.94% | 0.025 | 0.990 | no | FAIL |
| 2025-03-31 | +375.97% | +407.02% | 0.033 | 0.988 | no | FAIL |
| 2025-04-30 | +383.05% | +402.62% | 0.016 | 0.986 | no | FAIL |
| 2025-05-30 | +418.73% | +434.21% | 0.005 | 0.990 | no | FAIL |
| 2025-06-30 | +443.02% | +461.67% | 0.004 | 0.991 | no | FAIL |
| 2025-07-31 | +456.85% | +474.60% | 0.001 | 0.992 | no | FAIL |
| 2025-08-29 | +473.47% | +486.39% | 0.002 | 0.993 | no | FAIL |
| 2025-09-30 | +494.84% | +507.27% | 0.001 | 0.994 | no | FAIL |
| 2025-10-31 | +507.10% | +521.75% | 0.001 | 0.994 | no | FAIL |
| 2025-11-28 | +512.00% | +522.96% | 0.001 | 0.994 | no | FAIL |
| 2025-12-31 | +520.83% | +523.44% | 0.001 | 0.995 | no | FAIL |
| 2026-01-30 | +521.36% | +532.63% | 0.001 | 0.995 | no | FAIL |
| 2026-02-27 | +525.78% | +527.16% | 0.004 | 0.995 | no | FAIL |
| 2026-03-31 | +494.20% | +496.22% | 0.002 | 0.993 | no | FAIL |
| 2026-04-30 | +560.60% | +558.85% | 0.001 | 0.996 | yes | PASS |
| 2026-05-29 | +611.15% | +593.52% | 0.000 | 0.997 | yes | PASS |
| 2026-06-30 | +608.04% | +586.37% | 0.000 | 0.997 | yes | PASS |
| 2026-07-14 | +616.75% | +591.02% | 0.000 | 0.997 | yes | PASS |

## Summary

| strategy | return | SPY | EW eligible | Sharpe | max DD | turnover | rebalances | null p | DSR (n) | WF + | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| SUE/PEAD, PIT S&P 500 winner decile | +616.75% | +591.02% | +514.59% | 0.87 | -41.17% | 64.81% | 168 | 0.000 | 0.997 (2) | 4/4 | **PASS** |

Trial registry: **30 → 31** (one `pead-sue-tr` trial; family count now 2).

## Annual outcome distribution

> **History is not a forecast.** This is the DISPERSION a strategy like this has exhibited; the median is not a promise.

| year | strategy | SPY B&H | note |
|---|---|---|---|
| 2012 | +4.80% | +3.86% | partial (from 2012-07-02) |
| 2013 | +32.02% | +32.31% |  |
| 2014 | +18.49% | +13.46% |  |
| 2015 | +0.61% | +1.25% |  |
| 2016 | +15.35% | +12.00% |  |
| 2017 | +21.21% | +21.70% |  |
| 2018 | -4.03% | -4.56% |  |
| 2019 | +28.30% | +31.22% |  |
| 2020 | +13.40% | +18.37% |  |
| 2021 | +34.29% | +28.75% |  |
| 2022 | -10.00% | -18.17% |  |
| 2023 | +4.98% | +26.19% |  |
| 2024 | +17.61% | +24.89% |  |
| 2025 | +29.19% | +17.72% |  |
| 2026 | +15.45% | +10.84% | partial (through 2026-07-14) |

Block bootstrap: daily returns resampled in 21-session blocks, 1000 seeded draws of 252 sessions (seed 7); paired draws, same method both columns.

| percentile of simulated annual return | strategy | SPY B&H |
|---|---|---|
| 10th | -6.13% | -4.50% |
| 25th | +5.18% | +5.53% |
| median | +15.95% | +15.85% |
| 75th | +28.32% | +25.83% |
| 90th | +38.99% | +36.33% |

## Approval status

**None sought here — by design.** This is a VALIDATION run on a membership-gated universe of validation-only instruments (is_active=FALSE); it settles whether SUE/PEAD is a real, orthogonal alpha source on honest membership. It does not itself qualify any strategy for the approval workflow. Gates were not modified; no strategy row is touched.


---

## Pre-committed KILL-ONLY trial (start 2016-01-01, family pead-sue-tr-2016)

# THE EARNINGS-SURPRISE TEST — SUE/PEAD on the point-in-time S&P 500 (total)

> ## THE ONE ORTHOGONAL FACTOR THROUGH THE IDENTICAL BAR
> The ONLY difference from the momentum run is the signal: names are ranked by
> Standardized Unexpected Earnings (Foster-Olsen-Shevlin SUE), not 12-1 price
> momentum. Universe, delisting-aware engine, top-decile equal-weight monthly
> construction, monkey null, deflated Sharpe, purged walk-forward and the binding
> beat-SPY bar are REUSED BY IMPORT from xsmom_pit_run and the committed gauntlet.

> ## NO LOOK-AHEAD IS STRUCTURAL (signals/pead/v1.py)
> The report_date gates when a surprise is knowable; an after-market print is
> tradable only the NEXT session; the standardization of a report uses ONLY
> strictly-prior reports; and a report dated after the decision session is
> physically excluded from the ranking. A future report's numbers can be flipped
> wildly and the ranking at t is byte-identical (pinned by test).

Pinned spec (textbook, zero search): SUE = (epsActual - epsEstimate) / stdev(surprise over the prior 8 reported quarters); >= 4 priors required (else ineligible); drift-capture staleness window 63 sessions; EPS is vendor backward-split-adjusted to the current basis and used directly (no on-read adjustment). Winner portfolio is the top decile (max(10, n_eligible // 10)), equal weight, monthly.

- Evaluation window STARTS 2012-07-01 (membership-reliability bound); costs 5.0+5.0 bps/side; null 1000-path monkey MC drawing from the SAME PEAD-eligible set; purged walk-forward k=4, horizon=40, embargo=10; one registered trial per family; deflated Sharpe at the true count.
- Binding benchmark: SPY total return over the same window (ADR-0009); SPY carries no membership row and can never be ranked. Equal-weight-all-eligible shown, NOT binding.

## Data quality and honesty — earnings coverage

- Panel members with a usable price series: 657
- Members carrying >= 1 stored surprise: 637 (60109 completed reports on record; 53 of them delisted names — survivorship-free)
- Members that ever produce a standardizable SUE in-window: 637
- Members/eligible at each December rebalance: 2016: 424/410; 2017: 445/439; 2018: 460/453; 2019: 475/470; 2020: 484/474; 2021: 493/482; 2022: 496/488; 2023: 501/496; 2024: 502/494; 2025: 502/497
- Forced delisting liquidations during the run: 38; unfilled buys (died between decision and execution): 0

## Full-window result (start 2016-01-04, panel 2010-01-04 → 2026-07-14, 4156 aligned XNYS sessions, total return (split-adjusted; each dividend reinvested at its ex-date close — market_data/total_return.py))

Return +362.92%, Sharpe 0.85, max drawdown -41.17%, avg turnover 65.73% per rebalance, 126 rebalances

### Gate verdict: **FAIL**

- verdict: **FAIL**
- implication: SUE/PEAD does not clear the fund's bar on honest point-in-time membership; the graveyard verdict is recorded verbatim and the factor must not proceed toward approval (a failed gate is a deliverable, not a defect to be tuned away)
- strategy return: +362.92%
- SPY (BINDING benchmark per ADR-0009): +363.05%
- equal-weight all-eligible (informational, NOT binding): +287.41%
- null-model p-value: 0.000 (must be <= 0.05)
- deflated Sharpe: 0.988 at n_trials=2 (must be >= 0.9)
- trial registry id: `560710e8-bc2e-488c-9490-48412ce58a68` (family `pead-sue-tr-2016`)

Verbatim gate reasons:
- does not beat SPY buy-and-hold (362.9% <= 363.1%)

### Walk-forward: 4/4 folds positive

- fold returns: +59.99%, +43.07%, +3.04%, +82.63%
- mean return +47.18%, mean Sharpe 0.95, worst fold +3.04%

## Summary

| strategy | return | SPY | EW eligible | Sharpe | max DD | turnover | rebalances | null p | DSR (n) | WF + | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| SUE/PEAD, PIT S&P 500 winner decile | +362.92% | +363.05% | +287.41% | 0.85 | -41.17% | 65.73% | 126 | 0.000 | 0.988 (2) | 4/4 | **FAIL** |

Trial registry: **31 → 32** (one `pead-sue-tr-2016` trial; family count now 2).

## Annual outcome distribution

No distribution is derived for a failed strategy (house rule: earnings profiles are derived only for validated strategies — profit is a result to be discovered, never an input).

## Approval status

**None sought here — by design.** This is a VALIDATION run on a membership-gated universe of validation-only instruments (is_active=FALSE); it settles whether SUE/PEAD is a real, orthogonal alpha source on honest membership. It does not itself qualify any strategy for the approval workflow. Gates were not modified; no strategy row is touched.
