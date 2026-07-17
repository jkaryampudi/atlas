# ADR-0015 — PEAD sleeve suspended: budget to 0%, strategy stays on paper watch

Date: 2026-07-18 · Status: **Accepted** (signed by the Principal 2026-07-18) · Decider: Principal (Jay)

## Context
The implementable-variant backtest (board item 5, the open obligation of
ADR-0010 caveat 3 / ADR-0013; report
`docs/reports/implementable-variant-2026-07.md`, commit `26c1abb`) tested the
LIVE book shape — top-5 sleeves on a point-in-time large-cap universe — through
the identical gauntlet. Verdicts, verbatim:

- **Momentum survives and amplifies**: `xsmom-impl-tr` PASS, +2201.86% vs SPY TR
  +593.76%, p=0.000, 25/25 endpoints beat SPY (the decile validation showed
  8/25). The 2016 kill also passed.
- **PEAD does not transfer**: `pead-impl-tr` FAIL, null p=0.132 — random 5-name
  draws from the same recently-reported large-cap set perform as well; the SUE
  ranking carries no information at top-5 on this universe; 3/25 endpoints; the
  pre-committed 2016 kill also FAILED (p=0.139). This is the failure mode
  ADR-0013's caveats anticipated (edge recent, concentrated, overlapping
  momentum), now measured on the implementable form.
- The combined satellite's PASS is carried entirely by the momentum half.

## Decision
The Principal chose (my recommendation): **suspend the PEAD sleeve's capital.**
1. `SLEEVE_BUDGET_FRACTION["pead-sue-tr"]`: 0.10 → **0.00**. The operating
   allocation becomes **core 70% (SPY 55 / INDA 15) · momentum 10% · cash 20%**
   (amends ADR-0012/0014-B; L5 min-cash 0.10 remains a floor, now comfortably
   met).
2. The PEAD strategy row **stays `paper`**: signals keep generating, its top-5
   names keep going to the committee, memos keep being written and graded by
   the scorecard — the forward paper record continues to accrue at ~$1/night of
   desk spend. What changes is that its BUY memos size to zero (an honest
   recorded skip at the bridge), so no capital deploys behind them.
3. A dual-winner name (momentum AND PEAD top-5) deploys under the momentum
   sleeve alone — a zero-budget sleeve must not veto a funded one (bridge
   logic: budget-bearing families only govern sizing; membership in a
   zero-budget sleeve is recorded for attribution but allocates nothing).
4. **Reversal is a signature**: if PEAD's forward scorecard record (memo grades
   vs SPY, the dartboard baseline) vindicates it over a meaningful window, a
   new signed ADR can restore a budget. Nothing here deletes the strategy or
   its history.

## Consequences
1. The satellite is single-factor (momentum) at 10% until another candidate
   clears the implementable bar — narrow and deep, per ADR-0011.
2. PEAD becomes exactly what the evidence supports: a live forward experiment
   with zero capital at risk, judged by the same scorecard that judges
   everything else.
3. The derived PEAD bands and CUSUM keep running over its (empty) sleeve
   series — dormant until/unless a budget returns.
4. Registry note: the six implementable-variant trials are registered in dev
   (families `xsmom-impl-tr(±2016)`, `pead-impl-tr(±2016)`,
   `combined-impl-tr(±2016)`); the deflated-Sharpe counts include them.
