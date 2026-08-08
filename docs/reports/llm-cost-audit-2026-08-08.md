# LLM Cost Audit — prompt caching & Batch API (2026-08-08)

**Question:** can the Anthropic API bill be cut with prompt caching and/or the
Message Batches API? **Answer: not cheaply, and not yet.** Both require real
engineering on the desk's constitutional cage for small absolute savings; the
honest recommendation is to hold both until the scorecard decides whether the
desk earns its keep (~Aug 25), and take the one measured, zero-code improvement
available today (model registry). Numbers below are from the desk's own run
records, not estimates.

## Ground truth (research.agent_runs, the 2026-08-04 full desk night)

| Metric | Value |
|---|---|
| Calls | 74 (10 candidates: debate ×4, specialists, CIO, verifier) |
| Input tokens | 440,821 (avg 5,957/call) |
| Output tokens | 80,932 (avg 1,094/call) |
| Model | claude-sonnet-4-6 ($3/$15 per MTok) |
| Cost | ≈ $1.32 in + $1.21 out ≈ **$2.53/night ≈ $55/month** |

Input is ~52% of spend; output ~48%.

## Finding 1 — prompt caching: architecturally unavailable today

Caching is a **prefix** match. Atlas's prompt shape gives near-zero shared
prefix:

* Every prompt = `constitution.md` + role template (`runner.load_template`).
  The Constitution is the only cross-call shared prefix and measures **~264
  tokens — far below the 1,024-token cacheable minimum** for the current model
  (a `cache_control` marker would be a silent no-op).
* The big shared content — the per-candidate evidence dossier — is substituted
  *into* each role template **after** the role-specific text. `debate/bull.md`
  and `debate/bear.md` diverge at their first line, so across the ~7 calls per
  candidate the byte-identical prefix ends at the Constitution.

To cache, the templates would need restructuring (evidence corpus first, role
instructions after) — a **"Prompts are code" reviewed change** to every hashed
template, requiring red-team/eval revalidation since prompt order affects model
behavior. Upper bound of the win if ~60% of input became cache-reads:
~$0.71/night ≈ **$15/month**. Verdict: **poor ROI — do not do now.** Revisit
only if desk volume grows several-fold.

## Finding 2 — Batch API: real 50%, wrong place to spend risk today

The nightly desk is batch-shaped in *timing* (nobody waits overnight) but not
in *structure*: within one candidate the calls are strictly sequential (bear
rebuts bull; CIO reads both), so batching applies **across candidates,
per round** (all bull openings as one batch → all bear rebuttals → …).
That means refactoring `run_desk`/`run_agent` — the cage: schema-retry,
grounding verification, budget breaker, per-call audit events — from a
sequential loop into batched fan-out with polling. The 50% discount applies to
everything: ≈ $1.27/night ≈ **$27/month** at current volume.

Verdict: **defer to the scorecard decision (~Aug 25).** If the desk shows no
edge, the spend goes to ~$0 and this refactor is moot; if it shows edge and
scales up (more candidates, more analyze traffic), the discount grows with it
and the refactor is justified then. Refactoring the most safety-critical code
path for ~$1/day before knowing whether the desk survives is the wrong order.

## Finding 3 — the one genuinely free improvement (Principal decision)

The July shadow comparison (docs/reports/shadow-model-comparison-2026-07-19.md)
already measured **claude-sonnet-5 vs the incumbent sonnet-4-6** on the full
8-memo cohort: grounding/conviction/refs 8/8 on both, and **debate diversity
better on sonnet-5 (5/8 vs 1/8)**. Sonnet-5 lists at the same $3/$15 (intro
$2/$10 through 2026-08-31 ≈ $18 saved in August). Switching is **zero code** —
the per-role registry env (`ATLAS_MODEL_<ROLE>`) exists for exactly this.
Measured equal-or-better quality at equal-or-lower price; the registry switch
is a Principal decision per ADR-0005.

## Also noted

* `LlmResult`/`agent_runs` do not record `cache_read_input_tokens` /
  `cache_creation_input_tokens`. Irrelevant while caching is structurally zero;
  add the two fields in the same change that ever restructures prompts.
* Cost-cut levers that need no engineering remain available and reversible:
  desk cohort 10→5 (−50%), cadence nightly→Mon/Wed/Fri (−40%). Both slow
  scorecard sample accrual and are best decided with the Aug 25 verdict.

## Recommendation (revised from the 2026-08-06 discussion)

1. **Do now:** nothing code-side. Optionally the sonnet-5 registry switch
   (Jay's call — the shadow data supports it).
2. **Aug 25 (first scorecard verdict):** decide the desk's future. No edge →
   nightly desk off, Analyze-on-demand only (~$0 baseline). Edge → adopt the
   Batch API refactor as the scaling move, and only then consider the
   prompt-restructure that unlocks caching.
3. **Never:** churn the cage for ~$1/day.
