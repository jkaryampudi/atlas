# ADR-0002 — Quant rigour amendments (package v1.1)

Date: 2026-07-11 · Status: Accepted · Decider: Principal

## Decisions
1. **Trial registry**: every backtest ever run is registered (`quant.trial_registry`); Deflated
   Sharpe is computed against the true trial count for the strategy family. Validation reports
   missing a trial-count attestation cannot reach `approve`.
2. **Null-model gate**: strategy approval requires beating (a) buy-and-hold of its benchmark and
   (b) a >=1,000-path random-entry Monte Carlo using identical exits and costs, at agreed
   significance.
3. **Purged + embargoed walk-forward** is the backtester's default cross-validation.
4. **Single code path**: the backtester imports the identical strategy function production runs;
   enforced by import-linter and a live/backtest parity regression test.
5. **Implementation shortfall** (decision -> approval -> fill price) recorded per trade from the
   first paper session.
6. **Volatility targeting** at portfolio level (10-12% annualised target) as a Tier 1
   self-correction (see ADR-0003), bounded by the 20% cash floor (max gross 0.80).
7. **Factor-overlap guard** added to the risk engine (market/sector/momentum loadings), beyond
   pairwise correlation L8.
8. **CUSUM drift detection** on live-vs-backtest strategy returns; breach auto-demotes to paper.
9. **Tax-aware exit surfacing** (AU CGT discount comparison) in PM agent exit recommendations —
   informational, decisions remain human, treatment to be confirmed with an accountant.
10. **Cost drag** (brokerage, FX, data, LLM) is a first-class performance line and a managed item.

## Consequences
Docs 02/04/05/08 amended; migration 0002 adds `quant.trial_registry`; Phase 3/5 exit criteria
extended (canary must ALSO fail the null-model gate; parity test required before Phase 5).
