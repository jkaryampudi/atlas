# GPT_REVIEW_INPUT_GUIDE — how to read this evidence pass in four stages

> Audience: the independent reviewer (GPT-5.6 acting as IC / Chief Quant / CTO / CRO / Head of Data).
> This guide sequences the **evidence-pass** files (added on top of the original deep-dive package
> `00`–`20`). It anchors to `REPOSITORY_SNAPSHOT.md` (commit `2ba38c0`). Everything is classified with
> the 7-tag taxonomy in `EVIDENCE_BASE.md`; the **governing rule** throughout is *code existence is not
> verified behaviour*. You are meant to find what the internal pass missed — that gap is itself a
> finding (`17` Q21).

## Stage 1 — Triage (≈30 min): is this worth a deep review, and what is its honest maturity?
Read, in order:
1. `REPOSITORY_SNAPSHOT.md` — the exact commit/runtime/commands; what could and could not be run.
2. `EVIDENCE_BASE.md` — the classification taxonomy and the 20+ `EV-##` facts (which are executed
   vs claimed vs not-found). **Start here to calibrate trust.**
3. `FINAL_INDEPENDENT_REVIEW_READINESS.md` — the scorecard (10 dimensions, 1–10) and counts.
4. `01_EXECUTIVE_SUMMARY.md` + `16_KNOWN_LIMITATIONS.md` — the system's own honest framing.

**Triage question:** Is this a credible one-strategy research prototype with unusual process
discipline, or scaffolding around a fragile bet? Decide whether to spend Stages 2–4.

## Stage 2 — Quantitative validity (the core of the review): does the one edge survive?
The single validated strategy (`xsmom-pit-tr`, 12-1 momentum) carries the entire 40%-of-NAV book.
Interrogate it here:
1. `PERFORMANCE_EVIDENCE.md` — every metric, separated by stage; note there is **no paper/live track
   record yet** and the DSR at the honest trial count is **≈0.85 (below the 0.90 gate)**, reproduced.
2. `STATISTICAL_VALIDATION_GAPS.md` — what the gauntlet tests and the missing analyses (cost/slippage
   stress, parameter sensitivity, regime OOS — all NOT FOUND in code).
3. `QUANTITATIVE_ASSUMPTION_REGISTER.md` — 39 parameters; which are **ARBITRARY** (flat 10 bps, limit
   values, concentration); the normal-returns premise under the DSR; the CUSUM drift guard **inactive**
   on the flagship; live sleeve **top-5** vs backtest constant **TOP_N=10**.
4. `BASELINE_COMPARISON_REQUIREMENTS.md` — only **two** baselines are coded *and binding* (SPY-TR,
   selection-only monkey null); equal-weight is computed but non-binding; no clean single-variable
   ablation.
5. `POINT_IN_TIME_AND_BIAS_ANALYSIS.md` — the **two-universe / two-return-convention** split; the
   **inability to reconstruct the historical investable universe** (mandated MAJOR limitation);
   dividend ingest manual-only ⇒ the SPY-TR demotion bar decays.
6. `BACKTEST_REPRODUCIBILITY.md` — why **no** historical figure is reproducible from this snapshot
   (no per-run commit pin, optional/NULL data-snapshot, mutable single-vendor store, no `make` target).

**Stage-2 verdict to form:** Is the edge real and robust, or a single-regime, non-reproducible,
below-current-gate backtest? This is the decision the whole system rests on.

## Stage 3 — Engineering, safety & governance: can the machine hurt anything, and is the process real?
1. `EXECUTION_SAFETY_REVIEW.md` — the signal→order trace and the 8 safety questions. The strongest
   property: for paper, **the AI cannot move the book on its own** (no-agent-numbers wall tested;
   deterministic fail-closed bridge; human gate; fresh risk re-check that voids on FAIL). The material
   residual: **no API authentication** (EV-17).
2. `AI_AGENT_CONTROL_MODEL.md` — per-agent authority, self-validation, prompt versioning, and where
   LLM reasoning quality (not numbers) is the ungoverned surface.
3. `DOCUMENTATION_CODE_DISCREPANCIES.md` — where docs disagree with code (e.g. "Anthropic SDK" vs raw
   httpx; deployed-signal ≠ validated-signal; dead `MUTANT_no_such_state` risk overlay).
4. Cross-reference the existing deep-dives: `02` (architecture), `07` (risk), `14` (security), `15`
   (performance/ops).

**Stage-3 verdict:** Is the discipline load-bearing (audit chain EV-04, risk 100% branch EV-07,
no-agent-numbers EV-05) or cosmetic? Is the operational/security posture acceptable *even for paper*?

## Stage 4 — Source-code verification (trust nothing; re-run it):
1. `EVIDENCE_INDEX.md` — walk the `VERIFIED` rows (EV-01..EV-09, EV-19..EV-22) and **re-run the six
   safe commands yourself** (they are local, deterministic, need no licensed data):
   - `pytest tests/unit/test_boundaries.py` · `python -m atlas.tools.verify_chain` ·
     `pytest tests/unit/test_redteam_v1.py` · `pytest tests/unit/test_approval_recheck.py` ·
     `pytest --cov=atlas.dcp.risk --cov-branch --cov-fail-under=100` ·
     `python -m atlas.dcp.factory.recipe_run --spec <spec>.json --dry-run` · and the `deflated_sharpe`
     probe.
2. Spot-check the **exact-line findings** that don't need data: EV-21 (`signals/xsmom/generate.py:218-222`
   price formation vs `approve_xsmom_paper.py:105` total-return), EV-22 (`proposals.py:586-589`
   `MUTANT_no_such_state`), dividend-manual (`market_data/daily.py` has no `fetch_dividends`), DSR n-count
   (`registry.py` lineage_count → EV-06).
3. `19_FILE_INDEX.md` maps every claim to source; `20_APPENDIX.md` is the reference/glossary.
4. Close with `REMEDIATION_BACKLOG.md` — the prioritized findings, each with an acceptance criterion.

**Do NOT ask this package for:** an investment approval, a production-readiness sign-off, or licensed
data / secrets. Those are outside its scope by design — the decision is yours.

## One-screen map

| Stage | Files | Question answered |
|---|---|---|
| 1 Triage | REPOSITORY_SNAPSHOT, EVIDENCE_BASE, FINAL_..READINESS, 01, 16 | Worth reviewing? Honest maturity? |
| 2 Quant | PERFORMANCE_EVIDENCE, STATISTICAL_VALIDATION_GAPS, QUANTITATIVE_ASSUMPTION_REGISTER, BASELINE_COMPARISON, POINT_IN_TIME_AND_BIAS, BACKTEST_REPRODUCIBILITY | Does the one edge survive? |
| 3 Eng/Ops/Gov | EXECUTION_SAFETY_REVIEW, AI_AGENT_CONTROL_MODEL, DOCUMENTATION_CODE_DISCREPANCIES, (02/07/14/15) | Can the AI move the book? Is discipline real? |
| 4 Source verify | EVIDENCE_INDEX (+ re-run 6 commands), 19, 20, REMEDIATION_BACKLOG | Is the evidence true? What must be fixed? |
