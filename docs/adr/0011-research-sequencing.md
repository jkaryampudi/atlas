# ADR-0011 — Research sequencing: narrow and deep, one factor at a time

Date: 2026-07-15 · Status: **Accepted** (signed by the Principal 2026-07-15) · Decider: Principal (Jay)

## Context
An external quant review and a "v2 roadmap" proposed evolving Atlas from
single-factor momentum into a multi-factor, alternative-data, ML platform.
The roadmap's **guiding principles are correct and binding** — statistical
integrity over feature count; point-in-time only; no look-ahead; every
experiment registered; every improvement must beat the previous version; AI
never produces prices/weights/sizes; human is the final approver.

Several of the roadmap's *epics*, if built as a feature checklist, would
violate those principles: adding six factors at once, configurable/adaptive
factor weights, social-sentiment ingestion, and portfolio optimisation
assumed superior to equal-weight all re-introduce the overfitting and
attack-surface risks the principles exist to prevent. This ADR fixes the
SEQUENCE so capability grows without breaking the discipline that is Atlas's
actual moat.

Note: ~70% of the roadmap's "Epic 0 — Research Infrastructure" already
exists — the experiment registry is `quant.trial_registry` + the append-only
audit hash chain; walk-forward + Monte-Carlo validation + attribution are the
existing gauntlet; correlation and factor-exposure limits are in the risk
engine. It should be extended, not rebuilt.

## Decision
Atlas grows **narrow and deep, not broad**. The roadmap is a *hypothesis
backlog, not a build backlog*: each new factor, data source, or model is a
candidate that must clear the **unmodified gauntlet** — survivorship-free
point-in-time universe, total-return scoring, 1000-path null, deflated Sharpe
at the true trial count, purged walk-forward, and the binding beat-SPY-total-
return bar — before it touches capital, and **most are expected to fail**
(27 of 28 have). No composite score, regime-conditioned weighting, or machine
learning until **at least two orthogonal single factors have independently
cleared the bar**.

## Sequence (binding order)
0. **[in flight] Factor #2 — PEAD / earnings-surprise** through the full
   gauntlet; verdict recorded verbatim (PASS = second validated factor;
   FAIL = graveyard — both are deliverables). Strict earnings-*revisions*
   run separately as a **forward paper trial** (EODHD overwrites estimate
   history, so it is not point-in-time-backtestable; not decision-grade until
   the forward record accrues).
1. **Point-in-time Feature Store** (roadmap 0.2 — the genuinely new Epic-0
   work): versioned, reproducible, no-look-ahead store for every factor;
   migrate momentum and PEAD onto it. Extend `quant.trial_registry` with
   `hypothesis` and `dataset_version` fields (0.1 gap-fill).
2. **Specialist committee** (Epic 3): deepen the LLM desk into quality,
   growth, macro, and accounting analysts using evidence already wired
   (fundamentals, earnings, regime). Pure scrutiny upgrade — does not touch
   the "no agent numbers" wall, so it is architecturally free.
3. **Factors one at a time** (Epic 2): value, quality, growth, sector
   strength — each through the gauntlet, **survivors only**. Expect roughly
   one in four to clear the absolute long-only bar.
4. **Composite only when ≥2 orthogonal factors survive**: equal-weight, or a
   weighting scheme that is *itself* a registered, out-of-sample-validated
   experiment counted in the trial ledger. **Never a configurable dial.**

## Deferred, with reasons (not "later features" — gated candidates)
- **Portfolio optimisation** (MVO / Black-Litterman, roadmap 4.4): must be
  validated to beat the current equal-weight out of sample (DeMiguel et al.,
  "1/N"); MVO amplifies covariance noise into unstable weights and often
  loses — HRP is the only near-term candidate, MVO likely rejected.
- **Regime-adaptive factor weighting** (5.2): among the most overfit ideas in
  quant (easy to backtest, brutal live); admissible only via registered OOS
  validation, never a hand-set schedule.
- **Social sentiment** (6.3, Reddit/X/StockTwits): a prompt-injection attack
  surface on the LLM committee, not a weighting question — excluded until an
  injection-defence design exists. A low weight is not a defence.
- **Machine learning** (Epic 7): insufficient sample at the current ~112-name
  monthly universe (a few hundred stock-months/year) to fit tree ensembles
  without catastrophic overfitting; needs the feature store and a far larger
  universe first.
- **Exotic alternative data** (satellite, credit-card, options-flow beyond the
  basics): six/seven-figure data with thin evidence of surviving costs
  long-only; options and ETF flows are the only plausible near-term entries.
- **Live trading / broker execution** (8.5): a governance and regulatory step,
  human-armed, sequenced last (IBKR already technically connected).

## Parallel governance track (independent of factor research)
- Approval-contract refinement: percentile-derived tolerance bands + CUSUM
  auto-demote (currently provisional per ADR-0010).
- **Index-core allocation ADR**: deploy a passive core so capital is invested
  while factors prove out — the capital-preservation-consistent default
  (ADR-0009 reserves this as a Principal allocation decision).
- Off-laptop (Linux box) migration; operational hygiene — alert URL, key
  rotation, off-box backups, repository visibility.

## Consequences
1. The v2 roadmap's breadth is preserved as a menu; its ordering is replaced
   by this discipline-first sequence.
2. Each step is one well-scoped, gauntlet-gated deliverable, keeping the
   Principal in the loop between them.
3. "Every improvement must beat the previous version" becomes structural
   (the trial ledger + the absolute bar), not aspirational.
4. A factor joining the graveyard is a successful outcome of this process,
   not a failure of it.
