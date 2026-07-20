# ADR-0017: Satellite-heavy reallocation — retire the ETF core, momentum sleeve to 40%

**Status**: SIGNED
**Date**: 2026-07-20
**Supersedes**: the allocation split of ADR-0012/0014/0015 (70% SPY/INDA core /
10% momentum / 20% cash). Does NOT touch: the risk limit set (v2, unchanged),
ADR-0010's strategy approval + demotion bands, ADR-0009's approval bar,
ADR-0015's PEAD sleeve at zero.

## Context

The Principal directed (2026-07-20): no ETFs — individual stocks only. The
signed 70% SPY/INDA core was replication policy (ballast benchmarked to
itself, no alpha claim); it has never been executed (every core proposal
expired unapproved), so the book has sat 100% cash since inception. Of the
alternatives (direct-index stock core; satellite-heavy), the Principal chose
**satellite-heavy**: scale the one gauntlet-validated individual-stock
strategy and hold the remainder in cash. Sleeve size **40%** was chosen from a
menu bounded by the signed risk limits.

## Decision

1. **Momentum sleeve (`xsmom-pit-tr`, top-5, monthly): 40% of NAV** — up from
   10%. Per-name entry target 8.0% of NAV = exactly the signed L1 single-stock
   cap. `SLEEVE_BUDGET_FRACTION["xsmom-pit-tr"] = 0.40`.
2. **Core allocation: retired.** T8c stops proposing SPY/INDA; the
   `core_allocation` origin is closed to new proposals. No ETF positions.
   India exposure: none until a direct-NSE vendor decision (open item).
3. **PEAD sleeve: unchanged at 0** (ADR-0015 forward experiment).
4. **Cash: the remainder (~60% nominal)**, always ≥ 10% (L5).
5. **Everything downstream is unchanged**: the desk/bridge evidence rules,
   stop derivation (ADR-0006), demotion bands (ADR-0010: DD −40% / 126-session
   excess −25pp, latched), risk engine v2 limits, approval re-check.

## Costs, stated honestly (the Principal signs these, not just the upside)

* **Drawdown**: the strategy's own demotion band sits at −40% because its
  backtest lived there. At a 40% sleeve that is ≈ **−16pp of NAV** in a
  momentum crash, before the band demotes the strategy to suspended.
* **Cash drag**: with ~60% cash, the BOOK structurally lags SPY in up years —
  the sleeve must outrun SPY by ≈ 1.5× annually for the book to merely match
  it. Attribution will print that gap monthly. This shape cushions crashes
  and bleeds relative performance in bull markets.
* **Cap-edge friction**: entries target exactly the 8% L1 cap, so sizing §4
  may shave entries to fit, and appreciation between rebalances lifts names
  above 8% (L1 gates NEW proposals; it does not force-trim drift — drift
  resolves at the next monthly rebalance). Effective sleeve will often run
  under 40%.
* **Sector clustering**: momentum's top-5 frequently share a sector; L3 (25%)
  will then shave or refuse the 4th/5th name. The book holding less than
  target is the cage working, never a defect.
* **Concentration of validation**: the entire invested book now rests on ONE
  validated strategy. Its bands (tighten-only) and the null-model-gated
  graveyard are the reason this is defensible at all.

## Consequences

* `bridge.py` sleeve budget 0.10 → 0.40 (one constant, tests re-pinned).
* `core_allocation.maintain_core_proposals` → retired (T8c reports idle by
  policy; existing expired core proposals remain history).
* Console/README/System Note updated to the new book shape.
* The 20-session scorecard, bands, CUSUM and attribution keep measuring
  exactly as before; if the momentum sleeve breaches its bands it demotes to
  'suspended' and the book goes to cash — there is no fallback sleeve.

**Signed**: Jay Karyampudi (Principal), 2026-07-20 — "signed", after the costs block was read back verbatim.
