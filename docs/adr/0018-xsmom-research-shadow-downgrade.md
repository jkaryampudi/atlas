# ADR-0018: xsmom-pit-tr downgraded to RESEARCH_SHADOW — the independent-review verdict

**Status**: SIGNED
**Date**: 2026-07-20
**Amends**: ADR-0010 (xsmom-pit-tr paper approval + demotion bands) — the strategy
leaves operational `paper` for a non-authoritative `research_shadow` status.
Re-characterizes ADR-0017's 40%-of-NAV momentum sleeve as **non-authoritative
shadow exposure**. Relates to ADR-0009 (the approval bar re-promotion must meet)
and ADR-0016 (the lineage-count DSR defect this acts on).
**Does NOT touch**: strategy mathematics, factors, the `SLEEVE_BUDGET_FRACTION`
0.40 value, backtest results, historical performance files, or the risk limit
set. No backtest was re-run or re-scored for this decision — it acts on
already-signed evidence.

## Context

An independent institutional review (GPT-5.6 acting as IC / model validation /
risk / engineering, 2026-07-20; case file in `REVIEW_PACKAGE/`) returned
**"REJECT STRATEGY EVIDENCE"** for the *executable* xsmom-pit-tr sleeve. The
findings rest on evidence the fund had already signed or surfaced:

1. **Deployed signal ≠ validated signal.** The live generator
   (`atlas/dcp/signals/xsmom/generate.py`) ranks split-adjusted **price** return;
   the approval that minted `xsmom-pit-tr` validated **total** return
   (`atlas/tools/approve_xsmom_paper.py`). Same family name, two return
   conventions — for dividend payers the deployed edge is not the one that passed
   the gauntlet.
2. **DSR below the mandatory bar, grandfathered.** At the momentum-lineage trial
   count the flagship's deflated Sharpe is ≈0.85 < the 0.90 gate; ADR-0016 let the
   original n_trials=1 approval (DSR ≈0.999) stand. Reproduced directly from
   `deflated_sharpe(0.82, 3400, 23) = 0.853`.
3. **Not reproducible.** No per-trial code-commit pin and an optional/NULL data
   snapshot (`quant.trial_registry`); the historical +737.31% figure cannot be
   re-derived from an immutable code+data snapshot.

The review's Phase P0 recommendation: stop the invalid evidence from
accumulating — downgrade the strategy, freeze the sleeve as non-authoritative,
and gate re-promotion behind a fresh signed validation, **without** touching any
strategy math or historical number.

## Decision

1. **Downgrade.** `xsmom-pit-tr` moves from `state='paper'` to a new first-class
   `state='research_shadow'` (migration 0035; the actual flip is an audited
   one-shot, `atlas/tools/downgrade_xsmom_shadow.py`, that stamps `shadowed_at`
   and appends `quant.strategy.research_shadow` to the audit chain).
2. **No capital.** `research_shadow` is excluded from every tradability gate
   (`state IN ('paper','live')`), so signal generation, the sleeve budget, and
   the demotion bands stop treating it as authoritative. A **fail-closed bridge
   guard** (`_resolve_signal_ids`) refuses to build a proposal from any signal
   whose strategy is not authoritative — closing the hole where a residual signal
   would otherwise be sized by risk alone, uncapped.
3. **Never validated performance.** The reporting/API surface derives an
   `authoritative` / `validation_status` label from state
   (`atlas/dcp/strategy_lifecycle`); a `research_shadow` strategy is shown but
   labelled non-validated, never counted as validated paper performance.
4. **Fail-closed re-promotion.** A strategy carrying `shadowed_at` may return to
   `paper` only on a **signed validation artifact created after the downgrade**
   (`approval.require_signed_validation_artifact`) that meets the ADR-0009 bar.
   The stale pre-downgrade approval can never be reused, and there is no
   convenience-flag override.
5. **Operational hardening (P0).** The docker-compose API is bound to
   `127.0.0.1` (loopback only) — the unauthenticated control surface is no
   longer published on all interfaces. The full inventory of unauthenticated
   mutation endpoints is recorded in
   `docs/security/unauthenticated-mutation-endpoints.md`.

## What P0 deliberately does NOT do

- **No authentication** is implemented (a later phase; the inventory + the
  loopback bind are the P0 scope).
- **No new strategy** is built and **no parameter is tuned** — the 40% sleeve
  fraction, the risk limits, and every backtest number are byte-unchanged.
- **No historical file is edited.** The +737.31% / DSR figures stand verbatim in
  `docs/reports/`; this ADR re-characterizes their *status*, not their values.

## Consequences

- The invested book returns to **100% cash** in effect: with the momentum sleeve
  non-authoritative, no new momentum capital is deployed until a re-validation.
- The strategy's identity and history are preserved for **shadow observation**;
  forward evidence (a total-return-correct, reproducible, lineage-correct
  re-validation) is what could lift it back to `paper`.
- ADR-0017's satellite-heavy book stands as a *design*, but its single sleeve is
  now shadow — the fund's honest state is **zero validated deployed strategies**
  pending the P1/P2 re-validation the review specifies.

## To reverse (re-promotion path)

Re-validate the **exact executable** (total-return-correct generator, top-5
construction, stops/target, the deployed code hashed in full) on a reproducible,
lineage-correct, point-in-time gauntlet meeting ADR-0009; record a fresh signed
validation report; then `transition_to_paper` will pass the fail-closed gate.
Anything less is refused by construction.
