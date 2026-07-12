# ADR-0006 — Deterministic stop derivation: the memo→proposal bridge

Date: 2026-07-12 · Status: Accepted · Decider: Principal (Jay)

## Context
The research desk emits committee memos (recommendation, thesis, kill criteria,
dissent) nightly, but memos could not become trade proposals automatically:
`trading.trade_proposals` requires entry/stop/target prices, and Constitution
invariant 2 (CLAUDE.md) forbids any agent-produced number from reaching
sizing, pricing, or execution. Turning a BUY memo into a proposal therefore
requires a **deterministic price-derivation policy** computed by the DCP from
vendor data alone. That policy is a risk-shaping decision (the stop distance
drives L6 position sizing), so it belongs to the Principal.

## Decision
For a committee memo with `recommendation = BUY` (non-shadow), the bridge
derives, from vendor bars only:

- **entry** = latest EODHD close for the symbol;
- **initial stop** = `max(entry − 2 × ATR(14), entry × 0.90)` — volatility-
  scaled risk that L6 sizing then normalises across names, with a hard −10%
  floor so a high-volatility name cannot demand an outsized stop distance;
- **target** = `entry + 2 × (entry − stop)` (a 2R objective, recorded for
  review honesty; nothing executes on the target in v1);
- **quantity** = the risk engine's `size_position` (§4), unchanged — sizing
  remains an output of risk, never conviction.

ATR(14) is the 14-session Wilder average true range computed from vendor
high/low/close; fewer than 15 complete sessions of OHLC means **no proposal**
(fail closed, recorded as skipped).

Bridge scope guards: one live proposal per symbol (no bridge proposal while
the symbol has an open position, a pending/approved proposal, or a live
order); every bridge proposal flows through the unchanged lifecycle —
L1–L11 validation at build, human approval with fresh re-check, no bypass.

`signal_ids` (NOT NULL, non-empty) are populated with **deterministic UUIDv5
identifiers of the memo's DCP evidence refs** until `quant.signals` ships;
the ref→id mapping is recorded in the proposal's audit payload so the
lineage stays reconstructible. This is an acknowledged interim measure, not
signal provenance.

## Rationale
2×ATR is the standard volatility-aware initial stop for swing/position
horizons; the −10% floor caps the worst-case single-name risk consistent
with capital preservation; deriving every number from vendor bars keeps the
two-plane wall intact — the agent chose *what* to propose, the DCP alone
chose *the numbers*.

## Consequences
- BUY memos become sized, risk-checked proposals in the console approval
  queue automatically (daily cycle bridge step); REJECT/HOLD memos never do.
- The stop recorded at approval is the stop the exit engine enforces.
- Revisiting the multiplier/floor is a new ADR, not a code tweak.

Signed: Jay, 2026-07-12 ("signed off with your recommendation").
