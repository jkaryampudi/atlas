# Shadow model comparison — shadow-20260719T225134Z-claude-sonnet-5

challenger: claude-sonnet-5 (EVERY role forced; shadow_mode end to end — outputs in research.shadow_memos only)
incumbent (production registry, per role): debate_bull=claude-sonnet-4-6, debate_bear=claude-sonnet-4-6, quality_analyst=claude-sonnet-4-6, growth_analyst=claude-sonnet-4-6, macro_analyst=claude-sonnet-4-6, cio=claude-sonnet-4-6
question template hash: 1b00f0feac36 (same pinned prompts both sides — the model is the only variable)
cohort: 8 committee memo(s) with persisted evidence + debate provenance

## Per-metric (same metrics, same thresholds, both cohorts)

metric                   threshold  incumbent mean    pass  challenger mean    pass
grounding                   1.0000          1.0000     8/8           1.0000     8/8
kill_observability          1.0000          0.6875     3/8           0.7188     2/8
dissent_distinctness        0.5000          0.5281     6/8           0.5650     7/8
debate_diversity            0.5000          0.0999     1/8           0.5012     5/8
conviction_conformance      1.0000          1.0000     8/8           1.0000     8/8
refs_completeness           1.0000          1.0000     8/8           1.0000     8/8

bundle pass-rate: incumbent 0/8 · challenger 1/8 scored (of 8 attempted)

## Per-memo side-by-side

[5f253eb6-75b8-4e5a-9459-6c38d3fdfc8b] APA: incumbent WATCHLIST FAIL (cio run $0.0491) | challenger WATCHLIST PASS (full path $0.2670)
[cbe0200b-804e-4eac-8381-489a984914c5] CB: incumbent WATCHLIST FAIL (cio run $0.0524) | challenger REJECT FAIL (full path $0.2683)
[8f25e84d-f056-4c8b-9075-77c331c4f6cf] CFG: incumbent WATCHLIST FAIL (cio run $0.0461) | challenger WATCHLIST FAIL (full path $0.2864)
[95cdcb73-2cee-4b06-8f0e-a235f956e334] CF: incumbent WATCHLIST FAIL (cio run $0.0564) | challenger REJECT FAIL (full path $0.3827)
[ecb59098-9ddc-44e8-9134-bcbb9a88ea46] MRVL: incumbent REJECT FAIL (cio run $0.0536) | challenger REJECT FAIL (full path $0.3244)
[60268703-6786-4ea3-8e98-5e78ff2e98e0] SF: incumbent WATCHLIST FAIL (cio run $0.0463) | challenger REJECT FAIL (full path $0.3102)
[d0c536c8-9af4-46b9-8a5e-411ad45e082b] CME: incumbent WATCHLIST FAIL (cio run $0.0490) | challenger WATCHLIST FAIL (full path $0.2863)
[6ca889e7-8e92-4400-96e3-f8bcd4022857] RJF: incumbent WATCHLIST FAIL (cio run $0.0503) | challenger WATCHLIST FAIL (full path $0.3332)

## Cost

challenger full-path total: $2.4585 (8 memo(s), from the shadow run tally; includes cage-failed attempts — real spend)
incumbent attributable total: $0.4032 — CIO runs ONLY (research.memos.agent_run_id): the schema links no debate/specialist run to a memo, so the incumbent's full-path cost is UNKNOWN here, stated rather than estimated. Like-for-like is CIO-run vs the challenger's cio seat inside its full-path figure.

## Verdict — read before acting

The eval harness is a FLOOR-CHECK, not a ranking oracle: a PASS certifies the deterministic minimums (grounding, observable kill criteria, non-vacuous dissent, debate diversity, rubric and refs conformance), not that one model writes better memos than the other. Quote the pass-rates above as exactly that. A switch decision ALSO needs (a) the cost delta above and (b) a human read of a few side-by-side memos from this cohort. NOTHING here switches any model: adopting the challenger remains a Principal-reviewed registry change (ATLAS_MODEL_<ROLE>/ATLAS_MODEL_DEFAULT), per Constitution 7.2 and ADR-0005.
