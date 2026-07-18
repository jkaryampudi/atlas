# Shadow model comparison — shadow-20260717T141921Z-claude-sonnet-5

challenger: claude-sonnet-5 (EVERY role forced; shadow_mode end to end — outputs in research.shadow_memos only)
incumbent (production registry, per role): debate_bull=claude-sonnet-4-6, debate_bear=claude-sonnet-4-6, quality_analyst=claude-sonnet-4-6, growth_analyst=claude-sonnet-4-6, macro_analyst=claude-sonnet-4-6, cio=claude-sonnet-4-6
question template hash: 1b00f0feac36 (same pinned prompts both sides — the model is the only variable)
cohort: 8 committee memo(s) with persisted evidence + debate provenance

## Per-metric (same metrics, same thresholds, both cohorts)

metric                   threshold  incumbent mean    pass  challenger mean    pass
grounding                   1.0000          1.0000     8/8           1.0000     2/2
kill_observability          1.0000          0.8125     6/8           0.6250     0/2
dissent_distinctness        0.5000          0.6177     8/8           0.5388     1/2
debate_diversity            0.5000          0.3927     4/8           0.4026     1/2
conviction_conformance      1.0000          1.0000     8/8           1.0000     2/2
refs_completeness           1.0000          1.0000     8/8           1.0000     2/2

bundle pass-rate: incumbent 3/8 · challenger 0/2 scored (of 8 attempted)

## Per-memo side-by-side

[0feee8d6-3082-463e-915a-b66654131196] AMD: incumbent BUY PASS (cio run $0.0570) | challenger WATCHLIST FAIL (full path $0.4542)
[14eacc36-9b4c-4ee4-9512-4b9908fd871b] AMAT: incumbent BUY PASS (cio run $0.0536) | challenger WATCHLIST FAIL (full path $0.4281)
[3a463341-821a-49d7-bb4a-a458da760394] INTC: incumbent BUY FAIL (cio run $0.0514) | challenger BUDGET_HALT: surface budget breached (shadow): day total 3.03 > 3.00 USD sub-cap (ATLAS_BUDGET_SHADOW; global cap intact) (full path $0.1750)
[5f0671f7-0a60-4ae8-a950-6a3a3694a58c] CAT: incumbent BUY FAIL (cio run $0.0538) | challenger NOT_ATTEMPTED: budget exhausted — not attempted (full path $0.0000)
[647ff56e-0353-4a8a-8f84-55a5a17fbb52] LRCX: incumbent BUY PASS (cio run $0.0629) | challenger NOT_ATTEMPTED: budget exhausted — not attempted (full path $0.0000)
[16e4ef32-ad58-4640-a708-197633ff690e] WIT: incumbent REJECT FAIL (cio run $0.0474) | challenger NOT_ATTEMPTED: budget exhausted — not attempted (full path $0.0000)
[2b48476f-4aaa-4a66-9c20-c558df209170] T: incumbent WATCHLIST FAIL (cio run $0.0506) | challenger NOT_ATTEMPTED: budget exhausted — not attempted (full path $0.0000)
[3409f3e2-3975-413a-a79c-70cef0797161] CAT: incumbent BUY FAIL (cio run $0.0523) | challenger NOT_ATTEMPTED: budget exhausted — not attempted (full path $0.0000)

## Cost

challenger full-path total: $1.0573 (3 memo(s), from the shadow run tally; includes cage-failed attempts — real spend)
incumbent attributable total: $0.4290 — CIO runs ONLY (research.memos.agent_run_id): the schema links no debate/specialist run to a memo, so the incumbent's full-path cost is UNKNOWN here, stated rather than estimated. Like-for-like is CIO-run vs the challenger's cio seat inside its full-path figure.

!! PARTIAL RESULTS: the shadow budget sub-cap (ATLAS_BUDGET_SHADOW) halted the comparison mid-cohort. Unattempted memos are listed above; nothing was estimated.

## Verdict — read before acting

The eval harness is a FLOOR-CHECK, not a ranking oracle: a PASS certifies the deterministic minimums (grounding, observable kill criteria, non-vacuous dissent, debate diversity, rubric and refs conformance), not that one model writes better memos than the other. Quote the pass-rates above as exactly that. A switch decision ALSO needs (a) the cost delta above and (b) a human read of a few side-by-side memos from this cohort. NOTHING here switches any model: adopting the challenger remains a Principal-reviewed registry change (ATLAS_MODEL_<ROLE>/ATLAS_MODEL_DEFAULT), per Constitution 7.2 and ADR-0005.
