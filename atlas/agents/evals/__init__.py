"""Memo-quality eval harness (desk-review 2026-07 item 8, deterministic v1).

The PRE-OUTCOME judge: `atlas/dcp/scorecard.py` grades memos against what the
market later did (the outcome judge); this package grades memo QUALITY before
any outcome exists, so a prompt-template change can be scored against a frozen
fixture corpus before deployment instead of discovered in production.

THREAT MODEL (v1, pinned — round-4): the deterministic judge exists to catch
ACCIDENTAL quality drift by the fund's own agents under prompt-template and
model changes — the pre-deployment tripwire — NOT to defeat an adversarial
author gaming the judge. Deterministic lexical metrics cannot win a
word-order arms race against a cooperating adversary (round 3 proved it:
each added lexical guard fell to a one-clause rewrite), so v1 deliberately
stops escalating there. Deliberately-adversarial classes (reordered
partial-echo laundering, out-of-lexicon mood vocabulary, semantic
paraphrase) are DOCUMENTED LIMITS pinned as visible leaks in the corpus
(echo_chamber_reordered.json, the out-of-lexicon vibes test) and owned by
the deferred LLM-judge half of item 8, to be introduced under the same
shadow-mode discipline ADR-0005 established for model changes before it
gates anything. A PASS from this harness is a FLOOR, not a certificate.

Scope and discipline:

- DETERMINISTIC ONLY. Every metric is a pure function over a persisted memo
  bundle (memo + evidence provenance + debate provenance); no LLM is called
  anywhere in the harness or its tests. The LLM-as-judge half of item 8 is
  deferred deliberately — a judge you cannot replay is not a gate.
- REUSE THE CAGE'S TOKENIZER. The grounding metric calls
  atlas.agents.runtime.grounding verbatim (never a reimplementation), so the
  harness can never drift more permissive than the production verifier.
- FROZEN CORPUS. Fixture bundles live in `atlas/agents/evals/fixtures/`
  (inside the package, NOT tests/fixtures/) so `python -m
  atlas.agents.evals.run` scores the corpus from any checkout without the
  test tree, and so the corpus ships under the same prompts-are-code
  discipline: tests/unit/test_memo_eval_corpus.py sha256-pins every fixture
  file — editing the corpus is a reviewed diff that must break the pin.
- METRIC DEFINITIONS ARE CODE. Thresholds and lexicons are pinned constants
  in metrics.py; the corpus gate golden-pins every score, so loosening a
  threshold or a lexicon silently is structurally impossible (CLAUDE.md:
  never weaken a gate).
- READ-ONLY. The optional --db mode scores REAL persisted memos over
  research.memos / memo_evidence / memo_debate inside a transaction the
  database itself enforces as READ ONLY; it writes nothing and emits no
  audit events — measurement, not action.

KNOWN v1 LIMITS (eval-harness review 2026-07; documented here, in metrics.py,
and on every rendered report, so a clean sweep is never read as more than the
metrics measure):

- GROUNDING LIMITS ARE THE CAGE'S, BY DESIGN. The judge reuses
  atlas/agents/runtime/grounding.py verbatim, so it inherits the cage's known
  blind spots exactly: spelled-out word-numbers assert no numeric claim,
  ISO-date components in evidence ground small integers, and grounding is
  presence-not-attribution (cross-block substitution). These are NOT patched
  in the judge — a judge stricter than the cage would fail memos the cage
  certifies, and a judge looser would be a weakened gate. Hardening belongs
  in grounding.py itself: a cage change, reviewed separately, so both sides
  tighten together (finding #12).
- REBUTTALS ARE UNJUDGED. Diversity reads the two opening cases only;
  a rebuttal that capitulates produces no signal. v2 work (finding #14).
- CLOSED LEXICONS UNDER-CREDIT out-of-lexicon observable events ("stopped
  out", "delisted"); the pinned trade-off is deliberate false-strictness,
  and widening a lexicon is a reviewed, pin-breaking diff (finding #15).
- LEXICAL NOVELTY IS NOT MEANING. Boilerplate dissents and thesaurus-
  paraphrase debates pass the overlap metrics; verbatim, padded and
  ORDER-PRESERVING interleaved echoes do not (bigram/containment/novel-floor
  checks). REORDERED partial echo — clause-swapping the copied points over
  the same filler — passes, and is pinned VISIBLY as echo_chamber_reordered
  (round-3 finding #2). That class and semantic echo belong to the deferred
  LLM-as-judge (findings #7/#11, apex_bypass fixture, threat model above).

Entry point: `python -m atlas.agents.evals.run` (see run.py).
"""
