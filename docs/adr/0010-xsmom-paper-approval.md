# ADR-0010 — First strategy approval: xsmom to PAPER

Date: 2026-07-13 · Status: Accepted · Decider: Principal (Jay)

## Context
`xsmom-pit-tr` — cross-sectional 12-1 momentum, winner decile, monthly
rebalance, equal weight — is the first and only strategy to clear the full
ADR-0009 bar on honest data: point-in-time S&P 500 membership including 70
delisted names, total return vs SPY total return, after costs.

- Full window (2012-07…2026-07): **+737.31% vs SPY TR +593.89%**
  (margin +143.43pp), null p=0.000, DSR 0.999, purged walk-forward 4/4.
- Pre-committed kill test (2016 start): **PASS**, +377.32% vs +364.98%
  (margin +12.34pp), p=0.000, DSR 0.994.
- Verdicts recorded verbatim in
  `docs/reports/xsmom-pit-total-return-2026-07.md`; registry at 27 trials
  at decision time (the approval run itself registers one more — the
  registry only grows).

The Principal ordered the approval in writing on 2026-07-13: **"approve
xsmom on paper"**, after the caveats below were put in front of him with
the current live portfolio the recipe would hold.

## Decision
`xsmom-pit-tr` (`xsmom_pit` v1.0.0, code sha pinned on the strategy row) is
approved for **PAPER trading only**, executed via
`python -m atlas.tools.approve_xsmom_paper` — which regenerates the gate and
walk-forward artifacts in-process (deterministic, pinned seed) and refuses
on any missing or failing artifact. State machine:
backtested → validated (artifact-gated) → **paper** (this signature).
Live trading remains Phase 7: separate machinery, separate signature.

## Caveats accepted in ink (not waived — accepted)
1. **Endpoint concentration**: only 8/25 (full window) and 3/25 (2016 window)
   monthly-rolled endpoints beat SPY TR; the margin is concentrated in
   2026 H1 (the AI-semiconductor run). The strategy trails SPY TR in 8 of 13
   full calendar years. The gate says PASS twice; the shape of the win is
   what it is.
2. **Early-window membership undercount** (339 vs ~500 at 2012-07, decaying
   to zero) flatters the early years; 92% delisted-series coverage.
3. **Validated-universe vs trading-universe gap**: validated on PIT S&P 500;
   trades on the ADR-0007 universe (S&P 100 + India sleeve). The
   implementable-variant backtest (board memo item 5) remains OPEN and is
   the next quant deliverable. Until it lands, paper results are the
   bridge evidence.

## Guardrails (provisional bands — tighten-only)
Recorded on `quant.strategies.tolerance_bands` and enforced by the
approval-contract machinery (board item 7, being wired now):
- Sleeve drawdown from its own peak worse than **−40%** → auto-demote to
  `suspended` (backtest max DD was −36.9%).
- Trailing 126-session sleeve excess vs SPY TR below **−25pp** → auto-demote
  to `suspended` (outside anything in the 2012–2026 record).
- Demotion is machine-executed and latching; re-promotion is a Principal
  signature, never automatic.
- These bands are **provisional**: the approval-contract build must derive
  percentile bands from the stored backtest equity curve and may only
  TIGHTEN them. Loosening any band requires a new signed ADR.

## Consequences
1. `quant.strategies` gains its first `paper` row; desk evidence
   (`quant_evidence.py`) now renders the family as approved for paper —
   BUY memos citing xsmom signals become constitutionally possible.
2. Signal generation (`quant.signals`, migration 0020) and the daily cycle
   wiring land with this ADR so the paper book can actually be built;
   every proposal still passes L1–L11, sizing, and the Principal's
   Approval Queue — approval of the strategy is not approval of any trade.
3. The scorecard and the daily band check are the accountability loop:
   paper performance is graded against SPY TR from day one, and the sleeve
   demotes itself by rule if it breaks its record.
4. Monthly rebalance; between rebalances the sleeve holds; stops per
   ADR-0006 still apply to every position.
