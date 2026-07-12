# ADR-0009 — The approval bar: beat the market, absolutely

Date: 2026-07-12 · Status: Accepted · Decider: Principal (Jay)

## Context
Twenty-four registered trials produced a consistent pattern: several
strategy families beat randomness (trend filters, momentum ranking — real
effects, decisively out-of-sample), yet none beat buy-and-hold after costs
over the 2010–2026 window. Two of them (trend on QQQ/AVGO; xsmom on the
survivorship-free sector set) failed ONLY the buy-and-hold gate while
demonstrating materially shallower drawdowns. This forced the fund-identity
question: is the approval bar absolute outperformance, or does validated
risk-reduction (smaller crashes, lower return) also merit approval under a
capital-preservation mandate?

## Decision
**The bar is absolute: to be approved for trading, a strategy must beat
buy-and-hold (SPY total return over the evaluation window, after costs)
through the full unchanged gauntlet** — null model, deflated Sharpe at the
true trial count, purged walk-forward — exactly as the gates stand today.

## Consequences
1. Strategies whose validated value is risk-reduction without
   outperformance are NOT approvable. The graveyard's near-misses stay
   buried unless a future signed ADR revisits this bar.
2. In the absence of any approved strategy, the fund's honest default
   posture is cash and/or a deliberate passive index core. Whether to
   deploy the paper bankroll into such a core holding is an **asset-
   allocation decision reserved to the Principal** (separate from strategy
   approval; it would flow through the ordinary memo→proposal→seal path),
   not something any strategy machinery may decide.
3. The gates themselves remain untouchable except by signed ADR
   (working-style rule reaffirmed). This ADR *chooses* the existing bar; it
   does not modify any threshold.

Signed: Jay, 2026-07-12 ("ADR 0009 beat the market").
