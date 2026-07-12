# ADR-0008 — FX Research Sandbox: EUR/USD, sealed, quota-free

Date: 2026-07-12 · Status: Accepted · Decider: Principal (Jay)

## Context
The Principal asked for EUR/USD trading with self-improvement. FX is outside
the fund's mandate (Doc 03: US+India equities, long-only) and carries no
harvestable drift; the Principal's initial framing — "self-learn until it
generates A$50/day" — was REFUSED and is recorded here as refused: a learning
loop with a profit quota as its stopping rule converges on memorized noise
with probability ~1 (the backtest will always eventually show the quota; the
quota is the stopping rule, not a discovery). Profit is a result to be
discovered, never an input parameter.

## Decision
A sealed FX research sandbox is authorized, scoped as follows:

1. **EUR/USD only**, daily vendor OHLC (verified available 2010→present).
2. **Research-only, forever, under this ADR**: no live trading, no paper
   ledger shared with the equity book, no path to the risk engine, bridge,
   desk or approval queue. Promotion out of the sandbox would be a new,
   separate signed ADR.
3. **Sealed namespace**: sandbox code lives in `atlas/fxlab/` and its data in
   a dedicated `fxlab` schema; nothing in `atlas/dcp`, `atlas/agents`,
   `atlas/api` or `atlas/ops` may import from it (boundary-tested). It MAY
   reuse the evaluation discipline (thresholds, deflated Sharpe, trial
   registry) — trials are trials, and fxlab trials count in the same
   registry.
4. **Honest FX economics are mandatory in every simulation**: retail spread
   per side AND overnight swap/rollover on held positions, documented
   conservative constants. Long AND short (a pair position is always both).
5. **The benchmark is zero**: there is nothing to hold in FX; a strategy must
   beat doing nothing, after costs, through the full gauntlet (random-entry
   long/short null model, deflated Sharpe at the true registry count, purged
   walk-forward).
6. **"Self-learning" per ADR-0003's tiers**: measured-cost recalibration and
   structured loss post-mortems may run automatically; strategy changes ship
   only as new versions through the full gauntlet as new registered trials.
   No component may modify a strategy in response to its own P&L.
7. **No profit target exists anywhere in the sandbox.** If something passes,
   its earnings profile is derived and reported afterward (expectancy per
   trade, annualized Sharpe, drawdown, daily P&L dispersion) — whatever the
   numbers are.

## Consequences
- Most candidates are expected to FAIL; verdicts are recorded verbatim.
- The equity fund is structurally unaffected in every failure mode.
- Research attention is split — accepted by the Principal with the xsmom
  survivorship validation acknowledged as the equity priority.

Signed: Jay, 2026-07-12 ("signed").
