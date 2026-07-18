# ADR-0016 — Universe expansion: full S&P 500 + lineage-scoped trial counting

Date: 2026-07-18 · Status: **Accepted** (signed by the Principal 2026-07-18) · Decider: Principal (Jay)

## Context
The Principal directed extension of the trading universe from the ADR-0007
pinned snapshot (~105 active names) to the full S&P 500. Scouting (2026-07-18)
found the data essentially ready: all 502 current members carry deep PIT bar
history; nightly cost at ~500 active names fits in 7–12% of the vendor quota;
the desk/LLM budget does not scale with universe size (the shortlist stays
top-5 + top-5 + scanner). NIFTY 50 direct names were scouted in parallel and
found INFEASIBLE on the current vendor (zero NSE coverage at EODHD, no PIT
membership anywhere; a separate vendor-procurement decision, not part of this
ADR — India remains via ETFs/ADRs as originally signed).

Validation (ADR-0011 discipline — the live form must pass before activation):
**`xsmom-impl500-tr` — momentum top-5 on the FULL PIT S&P 500 (~497 eligible,
no liquidity screen, exactly the post-expansion live form) PASSED both
windows**, officially registered (registry 41→43):
- Full window: +2235.12% vs SPY TR +593.76%, null p=0.000 (1000 top-5 monkey
  draws from the identical eligible set), 25/25 rolled endpoints beat AND pass.
- Pre-committed 2016 kill: +991.82% vs +364.89%, p=0.001, 24/25 endpoints beat.
- Max drawdown −42.74% (milder than top-5-of-100's −51.97% — the wider tail
  diversifies the extreme drawdown).

## The counting defect this ADR also resolves
The adversarial audit of that validation confirmed a structural gap (board
memo item 9): the deflated-Sharpe gate counts trials PER FAMILY NAME
(ADR-0002 convention), so a freshly-named variant always evaluates at
n_trials=1 and the multiple-testing penalty cannot bind on any first-in-family
run. Recomputed at the momentum LINEAGE count (15 prior related trials), this
validation's kill test scores DSR ≈ 0.85 < 0.90 and would not clear the bar.

**Decision (Principal, 2026-07-18): lineage-scoped counting going forward;
this run stands.** All FUTURE gauntlet runs must compute deflated Sharpe at
the trial count of their full strategy LINEAGE (a registered lineage tag on
the trial registry; renaming a variant can never reset its penalty). This
validation is accepted under the family convention in force when it ran —
with the fact that its kill would not have cleared the lineage bar recorded
here, not hidden. No retroactive re-judgment of past verdicts.

## Decision — activate the full S&P 500 universe
1. **Activation is a built, reviewed mechanism** (no such path exists today;
   `sync_universe` deliberately never touches `is_active`): a one-shot tool
   flips the ~400 inactive current members active, RECONCILING against the
   vendor's `is_delisted` flag (the AGN trap: one "current member" is a dead
   ticker that must not be activated) and refusing any name without a bar
   series.
2. **GICS backfill first**: all 401 inactive member rows carry NULL
   `sector_gics`, which would break L3 sector-cap aggregation. Sectors are
   backfilled from vendor fundamentals before activation; names whose sector
   cannot be resolved are NOT activated (fail-closed).
3. **Catch-up ingest** (~5 sessions of bars + fundamentals + earnings
   calendar for ~400 names, ≈10,300 vendor credits one-time, then
   ≈6,900/night) happens on the first post-activation nightly; the quality
   gates stay untouched and will honestly RED any name the vendor cannot
   serve.
4. **Membership drift policy**: the universe is reconciled against index
   membership SEMI-ANNUALLY (or on Principal request) via this same reviewed
   mechanism — additions activated, deletions deactivated (positions in a
   deleted name exit via the normal discretionary-close path). No automatic
   day-to-day tracking; every reconciliation emits audit events.
5. The deploy lane is UNCHANGED: momentum top-5 (SLEEVE_MAX_NAMES=5), 10%
   sleeve, ADR-0006 stops, derived bands, all risk limits. What changes is
   the selection breadth the validated form draws from. PEAD remains
   budget-0 (ADR-0015); its signal universe widens with the expansion and its
   forward record continues.

## Consequences
1. Signal width grows (~50 winner rows/rebalance); desk cost does not.
2. Nightly ingest wall time grows ~4–5x inside the atomic transaction —
   acceptable on the current host, another argument for the Linux box.
3. The estimate-snapshot forward archive starts covering the new names only
   from activation day (history is unrecoverable — vendor overwrites).
4. Future factors face a strictly harder gate (lineage counting) — deliberate.
