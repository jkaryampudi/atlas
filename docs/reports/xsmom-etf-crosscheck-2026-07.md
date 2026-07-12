# Survivorship cross-check — xsmom recipe (12-1, top 3 of 9) on the Select Sector SPDR universe (2026-07)

> ## WHY THIS UNIVERSE IS SURVIVORSHIP-FREE
> The nine original Select Sector SPDR ETFs (XLB XLE XLF XLI XLK XLP XLU XLV
> XLY) have traded continuously since December 1998. Sector funds are never
> deleted for losing, and the sector set is fixed by construction — no
> index-membership churn, no winners selected into today's list, and the set
> was fixed decades before this test (no discretion = no selection bias).
> Sector/industry momentum precedent: Moskowitz & Grinblatt (1999), "Do
> Industries Explain Momentum?", Journal of Finance 54(4).
>
> **What this cross-check implies for the conditional S&P-100 result**
> (docs/reports/xsmom-momentum-2026-07.md): a PASS here means the cross-sectional
> momentum effect is real though the +4,584% S&P-100 magnitude remains inflated
> by survivorship; a FAIL here means the original PASS was likely a survivorship
> artifact. Either way the original verdict stays conditional until point-in-time
> constituents are tested (see the appendix).

> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)
> Full vendor history (2010-01-04 → 2026-07-10); the verdict is
> decision-grade FOR THE CROSS-CHECK QUESTION — pass or fail, recorded verbatim.

Validation-only universe: the nine ETFs are seeded with **is_active = FALSE** —
outside the tradable universe, the scanner, the desk and gate coverage (pinned
by test). The signed manifest (seeds/universe.json, ADR-0007) is untouched.

Same textbook recipe as the S&P-100 run (Jegadeesh & Titman 1993, 12-1, monthly,
equal weight, 252-session seasoning), zero parameter sweeps. The ONE
proportional adaptation: v1's top 10 of ~110 is the winner decile; 9 sector
funds take the winner third, top 3 of 9 (JT's construction is
fractional — the winner decile of the ranked universe — not an absolute count).
ONE registered trial (family `xsmom-etf`). Gate thresholds are IMPORTED from
the committed validation module — nothing restated, nothing tuned.

- Engine: portfolio target-weight, monthly rebalance at month-end close, execution at next session's open, costs 5.0+5.0 bps/side on turnover
- Null model: 1000-path monkey MC — at each rebalance, 3 names drawn uniformly from the SAME eligible set, identical engine/costs (ADR-0002 #2)
- Walk-forward: purged+embargoed on the daily timeline, k=4, horizon=40, embargo=10 (constants from real_run), warmup=252 (ADR-0002 #3)
- Registered in quant.trial_registry; deflated Sharpe uses the true family trial count (ADR-0002 #1)
- Benchmark: SPY buy-and-hold on a side panel sharing the identical session axis (SPY is deliberately NOT in the ranked universe); equal-weight all-9 shown per protocol, NOT binding
- Convention note (inherited from the round-2 machinery, applied identically to strategy, null, and both benchmarks): bars are split-adjusted PRICE returns — dividends/distributions are not reinvested on either side of the comparison

## Universe and data honesty

- Panel: 9 symbols included, 2010-01-04 → 2026-07-10 (4154 aligned XNYS sessions, split-adjusted)
- Included: XLB, XLE, XLF, XLI, XLK, XLP, XLU, XLV, XLY
- Excluded: 0 symbol(s) — per-instrument completeness rule (fail closed per series):

## Full-window result (start 2011-01-03, after 252-session seasoning)

Return +405.05%, Sharpe 0.69, max drawdown -31.95%, avg turnover 40.09% per rebalance (sum |Δw|, both sides), 186 rebalances

### Gate verdict: **FAIL**

- verdict: **FAIL**
- implication for the conditional S&P-100 result: the original S&P-100 PASS was likely a survivorship artifact and must not proceed toward approval on the strength of that run
- strategy return: +405.05%
- SPY buy-and-hold (BINDING benchmark — the fund's actual alternative): +482.57%
- equal-weight all-9, monthly (informational, shown per protocol, NOT binding): +349.31%
- null-model p-value: 0.045 (must be ≤ 0.05)
- deflated Sharpe: 0.997 at n_trials=1 (must be ≥ 0.9)
- trial registry id: `49c4f4bc-93dc-4c04-a1de-7c7ec61c5c6d`

Verbatim gate reasons:
- does not beat SPY buy-and-hold (405.1% <= 482.6%)

### Walk-forward: 4/4 folds positive

- fold returns: +53.25%, +26.57%, +72.09%, +65.19%
- mean return +54.27%, mean Sharpe 0.73, worst fold +26.57%

## Summary

| strategy | return | SPY B&H | EW all-9 | Sharpe | max DD | avg turnover | rebalances | null p | DSR (n_trials) | WF folds + | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| xsmom recipe, top 3 of 9 | +405.05% | +482.57% | +349.31% | 0.69 | -31.95% | 40.09% | 186 | 0.045 | 0.997 (1) | 4/4 | **FAIL** |

Implication: the original S&P-100 PASS was likely a survivorship artifact and must not proceed toward approval on the strength of that run.

Trial registry: **23 trials before this run → 24 after** (ONE xsmom-etf trial; family count now 1).

## Annual outcome distribution

No distribution is derived for a failed strategy (house rule: earnings profiles are derived only for validated strategies — profit is a result to be discovered, never an input).

## Approval status

**None sought here — by design.** This is a VALIDATION run on an untradable (is_active=FALSE) universe: it informs the conditional ADR-0007 xsmom verdict; it does not itself qualify any strategy for the approval workflow (dcp/backtest/approval.py). The gates were not modified; no strategy row is touched.
## Appendix — Prong B: EODHD point-in-time constituents probe (2026-07-12)

Probes run against the current plan with the configured API key; HTTP status,
payload sizes and key shapes recorded verbatim. No purchases, no scraping.

### What IS available on this plan

1. `GET /api/fundamentals/GSPC.INDX` (S&P 500) — **HTTP 200, 173,185 bytes**.
   Top-level keys: `Components`, `General`, `HistoricalTickerComponents`.
   - `Components`: 503 current constituents
     (`Code, Exchange, Industry, Name, Sector, Weight`).
   - `HistoricalTickerComponents`: **817 entries** with fields
     `Code, Name, StartDate, EndDate, IsActiveNow, IsDelisted`.
     Coverage, measured on the payload: `IsActiveNow` 502 / not-active 315;
     `IsDelisted` = 1 for 172 entries; earliest `StartDate` 1957-03-04;
     **143 entries have no `StartDate`**; `EndDate` present for 315 entries,
     earliest `EndDate` 2008-09-16 and only a handful before 2012 —
     i.e. removals before ~2012 are sparsely covered, and some rows are
     internally inconsistent (e.g. `AGN` IsDelisted=1 with `EndDate: null`;
     `AET`, `ALTR` delisted with `StartDate: null`).
2. `GET /api/eod/ABMD.US` (ABMD left the index 2022-12-22, delisted) —
   **HTTP 200**: full daily history is served for delisted tickers on this
   plan (verified January 2018 window, 2,465 bytes returned).

### What is NOT available on this plan

1. `GET /api/fundamentals/OEX.INDX` (S&P 100 — the ADR-0007 index) —
   **HTTP 200, 14,522 bytes**, top-level keys `Components`, `General` ONLY:
   **no `HistoricalTickerComponents`** for the S&P 100. Point-in-time
   membership of the exact ADR-0007 universe is not on this plan.
2. `GET /api/mp/unicornbay/spglobal/comp/GSPC.INDX` (marketplace S&P Global
   constituents add-on) — **HTTP 403** `Forbidden. Please contact
   support@eodhistoricaldata.com` (a paid add-on; out of scope — no
   purchases).

### What the definitive point-in-time test would require

- **Membership**: point-in-time constituent lists for the test universe. On
  this plan the only candidate is `GSPC.INDX` `HistoricalTickerComponents`
  (S&P 500, a superset of the S&P 100), usable ONLY with fail-closed handling
  of its gaps: restrict the window to ~2012→present (where `EndDate` coverage
  begins to look complete), drop-or-refuse entries with null `StartDate`, and
  registry-count the reconstruction as a trial. S&P 100 membership would
  require the marketplace add-on (403 above) or a licensed source (e.g.
  CRSP/Compustat).
- **Delisted-ticker price history**: available here (verified via ABMD), with
  one open policy question — a delisted series simply stops, so a documented
  terminal-value rule (cash-merger proceeds vs bankruptcy zero) is needed
  before any backtest holds a name through its removal.

### Note on the failure mode (facts already in the tables above)

The recipe beat the same-universe monkey null (p = 0.045 ≤ 0.05, DSR 0.997,
4/4 walk-forward folds positive) and lost only the BINDING gate: it did not
beat SPY buy-and-hold (+405.05% vs +482.57%). The sector ranking carried
some information relative to darts on the identical universe, and still was
not worth running against the fund's actual alternative. That nuance does not
soften the verdict — FAIL is FAIL, and the S&P-100 result remains
un-validated: with survivorship removed, the margin over SPY inverted
(+4,584% vs +483% there; +405% vs +483% here) and the null-model margin
narrowed from p = 0.000 to p = 0.045.
