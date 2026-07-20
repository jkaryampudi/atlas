# STATISTICAL_VALIDATION_GAPS — what the gauntlet tests, and what it doesn't

> Anchored to `REPOSITORY_SNAPSHOT.md` (commit `2ba38c0`). "Executed?" = executed *by this pass*.
> Historical gauntlet runs are **CLAIMED** (in-repo, not re-run here); only EV-06 (DSR arithmetic) and
> EV-08 (recipe determinism) were executed. Classification taxonomy per `EVIDENCE_BASE.md`.

## Table

| Validation method | Implemented in code? | Executed (this pass)? | Evidence | Result | Remaining gap |
|---|---|---|---|---|---|
| **Null model — 1000-path monkey**, seed 7, p≤0.05 | Yes (`xsmom_run.py:248,498-549`) | No (unsafe/runtime) | CLAIMED p=0.000 (ADR-0010) | Reported pass | **Single seed (7)**; the null randomizes selection/weights but capacity, timing, and cross-sectional structure of the null are worth an external quant's scrutiny; p=0.000 on one backtest ≠ forward edge. |
| **Deflated Sharpe ≥ 0.90** at lineage trial count | Yes (`validation.py deflated_sharpe`; `approval.py`) | **Yes (arithmetic)** — EV-06 | `0.999` @ n=1 (approval); **`0.853` @ n=23 (lineage)** | Passed at approval; **would fail today** | Approved value used n_trials=1; honest lineage count gives **≈0.85 < 0.90** (grandfathered, ADR-0016). DSR assumes **normal returns** — financial returns are fat-tailed/autocorrelated; the probability is mis-specified to an unknown degree. |
| **Purged + embargoed walk-forward** k=4, horizon 40, embargo 10 | Yes (backtest walk-forward) | No | CLAIMED 4/4 folds (ADR-0010) | Reported pass | Only **4 folds** on one asset panel; embargo=10 adequacy vs the 12-1 formation horizon is asserted, not derived; a single split scheme (no nested CV, no combinatorial purged CV). |
| **Overfit canary** (gate rejects a known-junk strategy) | Yes (canary test in suite) | No (not re-run this pass) | CLAIMED PASSED (CLAUDE.md P3) | Reported pass | A genuine strength (proves gates reject junk) but it is **one** canary; passing it bounds false-positives weakly, not the family-wide false-discovery rate. |
| **Multiple-testing / selection control** — lineage-scoped trial counting | Yes (`registry.py:63-70 lineage_count`) | Partial — EV-06 shows the n effect | Deflation uses lineage count | Working as designed | **NULL-lineage legacy rows are counted nowhere** (`registry.py:64-67`) → the true multiplicity may be *understated*; counting is per-lineage, not across the whole research program. |
| **Bootstrap CI on annual outcomes** (block bootstrap, seed 7) | Yes (`xsmom_run.py:498-618`, 21-session blocks, 1000 draws) | No | CLAIMED | Reported | Same-seed, same block length; block size (21) not sensitivity-tested; CI width not surfaced in the headline. |
| **Cost/slippage stress** | **No** (flat 10 bps/side only) | — | NOT FOUND | — | No slippage/impact/spread model; a monthly concentrated rebalance is exactly where costs bite (Q3). Unmodeled. |
| **Parameter sensitivity / robustness** (top-N, weighting, lookback) | **No** | — | NOT FOUND | — | The top-5 / 12-1 / equal-weight choices are **not** shown robust to nearby values (Q2). Concentration may be overfit. |
| **Regime / subperiod robustness** | Partial (measured-over-time only) | No | PLANNED (Q1) | — | Sample is "largely bull" (Q1). No bear-regime OOS. The single most important open question. |
| **True out-of-sample (paper) significance** | n/a | No | Zero paper days (EV-absent) | — | No forward statistical evidence exists yet. |
| **Data cross-validation (2nd vendor)** | **No** (single vendor EODHD) | — | NOT FOUND | — | An internally-consistent bad datum passes the coarse RED/AMBER gate; no cross-source reconciliation. |
| **Normality / stationarity tests** underpinning the DSR | **No** | — | NOT FOUND | — | The DSR's normal-returns premise is never tested against the actual return distribution. |

## The gaps that most threaten the headline (ranked)

1. **DSR ≈ 0.85 at the honest trial count** — below the gate; grandfathered. The strongest single
   statistical caveat, reproduced by EV-06. Everything downstream inherits it.
2. **No cost/slippage stress and no parameter-sensitivity study** — the two analyses a committee
   would demand before trusting a concentrated monthly strategy; both **NOT FOUND** in code.
3. **One regime, no forward OOS** — the sample is largely a bull market and there is zero paper track
   record; statistical significance on one in-sample backtest is not evidence of a forward edge.
4. **Single seed, single split scheme, single vendor** — determinism is strong (a virtue for
   reproducibility) but it means the reported p-values and fold-passes are *one draw*, not a
   distribution over experimental choices.

## What is genuinely strong (do not understate)
- The gauntlet **is** multi-pronged (null + DSR + purged walk-forward), **lineage-scoped** trial
  counting resists the rename-to-reset-the-penalty attack (`registry.py:1-10`), and the **overfit
  canary passes** — the machinery to reject junk exists and the graveyard (6 of 9 lineages killed)
  shows it fires. The gaps above are about *coverage and calibration*, not *absence of rigor*.
