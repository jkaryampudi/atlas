# PERFORMANCE_EVIDENCE — every number, separated by validation stage

> Anchored to `REPOSITORY_SNAPSHOT.md` (commit `2ba38c0`). **Rule (task spec): never combine
> in-sample, validation, out-of-sample, walk-forward, paper, and live results into one figure.** Each
> metric below is tagged with the single stage it belongs to. **No figure here was reproduced by this
> pass** — all performance numbers are **CLAIMED** (they exist in the repo/ADRs, not re-derived here);
> see `BACKTEST_REPRODUCIBILITY.md`. This pass invented no numbers.

## 0. The one-sentence honest summary
Atlas's entire performance record is **a single historical backtest of one strategy** (`xsmom-pit-tr`,
12-1 momentum). There is **no out-of-sample paper track record yet** (the book was 100% cash at review
time), and **no live record** (paper-only). Everything below must be read in that light.

## 1. Stage taxonomy used

| Stage | Meaning here | Present in Atlas? |
|---|---|---|
| In-sample / full-period backtest | The whole-history simulated run | **Yes** — the +737% figure |
| Null-model validation | 1000-path monkey null, seed 7, p≤0.05 | Yes |
| Deflated-Sharpe validation | DSR probability ≥0.90 at lineage trial count | Yes |
| Purged walk-forward (OOS-ish) | k=4 folds, horizon 40, embargo 10 | Yes (folds reported) |
| Paper (true OOS, forward) | Realized paper-broker P&L | **No — zero days** |
| Live | Real capital | **No — paper-only, unbuilt** |

## 2. The metrics, by stage (all CLAIMED unless noted)

| Metric | Value | Period | Dataset | Validation stage | Costs incl. | Evidence | Reproducible this pass? |
|---|---|---|---|---|---|---|---|
| Strategy total return | **+737.31%** | ~2012→2026 (per Q1: "largely bull") | EODHD S&P 500 PIT, delisting-incl. | **In-sample / full backtest** | Yes — flat 10 bps/side | CLAIMED (CLAUDE.md, ADR-0010) | **No** (EV-11; no commit+data linkage) |
| Benchmark (SPY) total return | **+593.89%** | same | EODHD SPY TR | Benchmark for the above | Yes | CLAIMED | No |
| Excess vs SPY-TR (absolute bar, ADR-0009) | **+143.4pp** | same | — | Derived from the two above | Yes | CLAIMED | No |
| Null-model p-value | **p = 0.000** | full | 1000 monkey paths, seed 7 | Null-model validation | n/a | CLAIMED (`xsmom_run.py` null) | Not run (unsafe/runtime) |
| Deflated Sharpe (at approval) | **0.995** (≈0.999) | full | — | DSR validation, **n_trials=1** | n/a | CLAIMED; **arithmetic reproduced** — EV-06: `deflated_sharpe(0.82,3400,1)=0.9987` | Arithmetic only |
| Deflated Sharpe (at lineage count) | **≈0.85** | full | — | DSR validation, **n_trials=23** | n/a | **Reproduced arithmetic** — EV-06: `deflated_sharpe(0.82,3400,23)=0.8532` | Arithmetic only |
| Purged walk-forward | **4/4 folds pass** | full, k=4 | PIT panel | Walk-forward (OOS-ish) | Yes | CLAIMED (ADR-0010) | Not run |
| Paper realized P&L | **None (0 days)** | — | — | Paper (true OOS) | — | VERIFIED-absent: book 100% cash at review (`01`,`README`) | N/A — nothing to reproduce |
| Live P&L | **None** | — | — | Live | — | PLANNED/unbuilt (EV-16) | N/A |

## 3. The three things a reviewer must not let this table hide

1. **It is one backtest, not a track record.** Rows 1–7 are all derived from *the same single
   historical simulation of the same single strategy*. They are **not** independent confirmations;
   the null-model, DSR, and walk-forward are *stress tests of that one backtest*, not separate
   out-of-sample periods. The only true out-of-sample stage (paper) has **zero** data.

2. **The DSR at the honest trial count is ≈0.85 — below the 0.90 gate.** EV-06 reproduces this from
   the function directly: at the lineage-scoped count (n=23) the flagship's deflated Sharpe is
   `0.8532`, i.e. it **would not clear today's bar**. It was approved at n_trials=1 (`0.999`) under
   ADR-0010 and is *grandfathered* per ADR-0016. This is disclosed in the package (`00`,`17` Q6) and
   is the most important quantitative caveat on the headline. The 0.90 gate is itself a probability
   under a **normal-returns assumption** (EV-06), which financial returns violate.

3. **The deployed signal ≠ the validated signal.** Per the internal audit (package `01`§6, `16`), the
   live ranker uses split-adjusted **price** return while approval used **total**-return formation, and
   dividend ingest is manual — so even once paper P&L accrues, it will measure a signal that differs
   from the one that passed the gauntlet, for dividend payers. `STRATEGY_IMPLEMENTATION_TRACEABILITY.md`
   traces this; treat the forthcoming paper numbers accordingly.

## 4. What performance evidence would raise the confidence bar
- A **reproducible** flagship run (commit + immutable data snapshot pinned) matching the cited figures.
- **Forward paper P&L across a drawdown/regime shift** with the demotion band holding (Q1) — the
  single most decision-relevant future evidence, arriving on a calendar, not a build.
- The DSR re-stated and **defended at the true lineage trial count** (≈0.85), or an explicit,
  signed risk acceptance of the grandfather clause.
- A cost-model stress (beyond flat 10 bps) and a top-N / weighting sensitivity (Q2) — see
  `STATISTICAL_VALIDATION_GAPS.md` and `QUANTITATIVE_ASSUMPTION_REGISTER.md`.
