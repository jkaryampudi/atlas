# ADR-0003 — Closed-loop learning with governed deployment (package v1.2)

Date: 2026-07-11 · Status: Accepted · Decider: Principal

## Decision
Atlas becomes self-learning and self-correcting under a three-tier autonomy model
(Constitution Article 10):

- **Tier 1 — automatic self-correction** within pre-registered bounds: vol-target exposure
  scaling, regime transitions, cost-model recalibration from realised fills, ATR-tracked stop
  distances, in-bounds strategy re-fits, agent conviction-weight updates via shrinkage.
  Every adjustment is an audit event and reversible.
- **Tier 2 — self-learning proposals**: out-of-bounds re-fits, new strategy hypotheses mined
  from the platform's own decision journal, prompt refinements, sleeve reallocation. All flow
  through existing validation/change-control gates.
- **Tier 3 — never self-modifying**: risk limits, Constitution, breaker thresholds, activation
  of anything new. Human, dual-confirmed.

## Learning substrate
New `learning` schema: outcome labels (thesis-scored closed positions with P&L decomposition),
counterfactual ledger (rejected/expired proposals and stopped positions tracked forward),
agent calibration (Brier scores per agent per regime -> conviction weights), lessons memory
(structured post-mortems retrieved into future committee context), and an adjustments log.

## Guardrails
Shrinkage on small samples; bounded quarterly sleeve reallocation; every adjustment reversible
to a prior version; `learning` freeze scope in the halt system; monthly compliance section
listing all self-adjustments. Phase 5 exit gains: one full learning cycle demonstrated on paper
(miscalibration detected -> adjustment -> measured improvement).

## Honest constraint
At swing-trade frequency, statistical learning is slow (~40-80 closed positions/yr). The
counterfactual ledger and qualitative lessons memory carry early value; Bayesian allocation is
not expected to be meaningful before 12+ months of history.
