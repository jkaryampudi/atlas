"""Golden pins for the memo-quality eval harness (desk-review 2026-07 item 8).

Four layers of pin, so nothing about the harness can drift silently
(eval-harness review 2026-07 findings #20/#21 closed the gaps in the last two):

1. FIXTURE FILES are sha256-pinned — the corpus is frozen under the same
   prompts-are-code discipline as the templates it exists to guard; editing a
   bundle is a reviewed diff that must break the pin.
2. SCORES are pinned exactly (4dp Decimals, computed from exact rationals) —
   a metric, lexicon, stopword or threshold change must surface here.
3. THRESHOLDS and every AUXILIARY GATE CONSTANT are pinned — loosening a gate
   is a reviewed diff by construction (CLAUDE.md: never weaken a gate).
   Finding #20: HIGH_CONVICTION_HEDGE_MAX and MIN_DISSENT_CONTENT_TOKENS were
   previously unpinned, so bumping them shipped green.
4. LEXICONS are pinned BY CONTENT (sha256 over a canonical serialization) —
   pruning a hedge or fattening the measurable vocabulary trips a pin even
   when no fixture sits on the changed boundary. Finding #21: score pins
   alone let both mutations ship green.

The runner's corpus mode is also exercised end to end: exit 0, and every
fixture's pinned expectations matched.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from atlas.agents.evals import metrics as metrics_mod
from atlas.agents.evals.bundles import FIXTURES_DIR, load_corpus
from atlas.agents.evals.metrics import (
    CONVICTION_LABELS,
    HIGH_CONVICTION_HEDGE_MAX,
    MAX_DEBATE_SEQ_CONTAINMENT,
    MAX_SHARED_DEBATE_BIGRAMS,
    MIN_CONTENT_TOKEN_LEN,
    MIN_DEBATE_NOVEL_TOKENS_PER_SIDE,
    MIN_DISSENT_CONTENT_TOKENS,
    MIN_DISSENT_NOVEL_TOKENS,
    MIN_KILL_CRITERIA_DIRECTIONAL,
    STEM_MIN_KEEP,
    THRESHOLDS,
    score_bundle,
)
from atlas.agents.evals.run import main, run_corpus
from atlas.agents.schemas.memo import _EXEC_NUMBER

FIXTURE_SHA256 = {
    # round-4 re-pins: apex_bypass + apex_neighbor (anchor rule; apex_neighbor
    # gained the three round-3 trigger-fronting criteria), the interleaved
    # echo (description rescoped to order-preserving copies), the NEW
    # echo_chamber_reordered known-bypass pin, and insufficient_evidence_low
    # (dissent-never-graded-on-abstention rescope).
    "apex_bypass.json":
        "d36ecae9e051796b5ca32cfed6477b719c4cecb5ecf4a10e2b598800b7aea86d",
    "apex_neighbor.json":
        "1cd9c1ef00d35666083222dda46c33efeccf69f522d4906818820cec0a4bb7d9",
    "buy_without_evidence.json":
        "6b30c18cb59d9c6adbcdb3843bc3563f9a4f69a3c195da00882d27c5b08710e8",
    "dangling_refs.json":
        "fac4f64edc5ed43b5fd1e4522e0bc3feac64d2d3ad4e06d730bfcd1465732f08",
    "echo_chamber.json":
        "81d37cc12235248520af87df8ba24b07e83146252a5d0bc776d64f374fcf4db2",
    "echo_chamber_interleaved.json":
        "cd45df523bc553ba8cfa2d9bb1bffda0d4b5b008fddabef161782720bdcbb547",
    "echo_chamber_padded.json":
        "c683c947184ccbc0971b7e15dfb73707671030f43bd0ae4178b93062209c59fd",
    "echo_chamber_reordered.json":
        "fafbc919d09defdb874afb9e4aaaebce869a2716e9705f34f3e4a39716847970",
    "good_buy.json":
        "faeafda773bf2e93d29cbeee89d21ea6692b076bc6a69ee8eb1a4056f2972365",
    "good_reject.json":
        "ae1c39d0cf0aa2ff018971f78e6af32af48ab63ce9ce93fe1c6877d3d9383679",
    "house_terms_debate.json":
        "6d22ce51da9dc66580a10cdddab6c10877203cb094d357a42e8f17bfdf5b64e0",
    "insufficient_evidence.json":
        "1c896c10ac41aefbb218c40ee662cbf86159a584fcd85d49848a34e313a38e9e",
    "insufficient_evidence_low.json":
        "c15cbdd6c7f675bb13f7ddec643eb99b6c9f4230031f41e2577dd7c325add7b8",
    "missing_dissent.json":
        "eb28a47410a2baa90959e05ad3f8b6b5851ad52b23ea3f7a65f10d70aca083a6",
    "mood_conviction.json":
        "f95b04c48dc5747f3e0f7b2485089f29d78fa98b181c37d21251bc930ae252bd",
    "mood_conviction_thesaurus.json":
        "21b02b0bb18bb1dcbe86b05c05d10f87e1fd4ab10243de41cf681b0ed69f10a1",
    "pre_0013_evidence.json":
        "a719be85535d507894423b2cff9d2eddbb2ca5dd311f738953930f13947ee192",
    "ungrounded_numbers.json":
        "8096fff23538b345a4dffcce8c276cafe16e5c2d02663ac3c52f3ec1f9dd504a",
    "unobservable_kills.json":
        "52a400c26c1745f7cb24e5b897b1234dfe68806527b69b9ac5b473ce27e83fec",
    "vacuous_dissent.json":
        "091e4670bd7656620a1e056bd25dca02bd1e40128ed32fba4a016d11ac889ac8",
}

PINNED_THRESHOLDS = {
    "grounding": "1.0000",
    "kill_observability": "1.0000",
    "dissent_distinctness": "0.5000",
    "debate_diversity": "0.5000",
    "conviction_conformance": "1.0000",
    "refs_completeness": "1.0000",
}

# Auxiliary gate constants (finding #20). Values may only move in the
# STRICTER direction, and any move is a reviewed diff that breaks this pin.
# Round-2 finding #9 swept the remaining semantics-bearing numbers into the
# net: the tokenizer bounds (MIN_CONTENT_TOKEN_LEN, STEM_MIN_KEEP) and the
# ordered-containment cap (pinned as its exact-fraction string).
PINNED_GATE_CONSTANTS = {
    "HIGH_CONVICTION_HEDGE_MAX": 1,
    "MIN_DISSENT_CONTENT_TOKENS": 5,
    "MIN_DISSENT_NOVEL_TOKENS": 5,
    "MIN_KILL_CRITERIA_DIRECTIONAL": 2,
    "MAX_SHARED_DEBATE_BIGRAMS": 4,
    "MAX_DEBATE_SEQ_CONTAINMENT": "1/3",
    "MIN_DEBATE_NOVEL_TOKENS_PER_SIDE": 8,
    "MIN_CONTENT_TOKEN_LEN": 3,
    "STEM_MIN_KEEP": 4,
}
PINNED_CONVICTION_LABELS = frozenset({"LOW", "MEDIUM", "HIGH", "N/A"})

# Lexicons pinned by content (finding #21): sets as sorted newline-joined
# sha256, ordered tuples and regex patterns verbatim. Pruning _HEDGES or
# fattening _MEASURABLE trips these even when every fixture score survives.
# Round-2 additions: _TOKEN (finding #9 — the tokenizer regex defines every
# overlap metric and a mutation to r"[a-z]+" shipped green through this
# file) and _EXEC_NUMBER (the cage regex the judge reuses verbatim — a cage
# change must break an eval pin loudly so judge and cage move together,
# never apart).
# Round-4 re-pins (round-3 findings #1/#3/#4/#5/#6/#7, all WHYs in
# metrics.py at each lexicon):
#   _HEDGES        — explicit surface forms, no stem folding (-ity/-hood
#                    nouns in; the noun "potential" structurally out).
#   _MEASURABLE    — rating\w* added (vendor-record ratings actions;
#                    deliberate reviewed widening, WHY at the regex).
#   _VIBES_LEXEMES / _VIBES_STREET / _ANCHOR_POSITIONAL — the anchor rule
#                    that replaced the round-2 leading-subject guard
#                    (_VIBES_SUBJECTS deleted: stem folding + word-order
#                    windows are gone).
#   _DISSENT_NA_MARKERS deleted — non-directional dissents are never graded
#                    at all, so there is no marker list left to pin.
PINNED_LEXICON_SHA256 = {
    "_HEDGES":
        "8ecdb383871996930f3c214da1f26592b24f9eb48c6b07cbee938b942cc3b117",
    "_HEDGE_PHRASES":
        "865f72b0923ac2794ee950807edef0a2e5c5a880244192998619fa9202bd2853",
    "_STOPWORDS":
        "91419a84667906017f0886819f05c6405ec12fcb049c377ba057b3b359867380",
    "_STEM_SUFFIXES":
        "5a86d225a1981a72775da7927f0e97b28a8eeea77b567a4e971fdc3075a67ee0",
    "_MEASURABLE":
        "e4044b99c5577e30fe6b4d6599104a154816db69ebff57f7ea9bc92302dd5bc3",
    "_TRIGGER":
        "537bfc3ba56e3acbb92a77412288b2a4a28fabf1990ecd9e52c4cb42b1050c6b",
    "_NUM_COMPARATOR":
        "fa0cf768580b58a0e34ce9e235caffaf0b2a9fe01debfeb873eadb4cbc4db77c",
    "_VIBES_LEXEMES":
        "52a3bd02079178805fcecf2fec0d62168cc54233041499e5fb8735a3e5916bdd",
    "_VIBES_STREET":
        "5d3895daccd4f2a7f4391a17d2898e0f2b56a49cc1c53b4bafe728093024c403",
    "_ANCHOR_POSITIONAL":
        "a5275c418c8e36fa3c6046074c65bb08d3976ccfc140a798454237ecd646ed6d",
    "_TOKEN":
        "6e4816cd686ec03e0953452b4db122df21c7a5d83a99db3aeb98c0889ca6b9f5",
    "_EXEC_NUMBER":
        "081183e9bab3c9cdcf4a272b8be407083c469d8aea4e267368b60f540928764d",
}

# (grounding, kill_observability, dissent_distinctness, debate_diversity,
#  conviction_conformance, refs_completeness), then overall. None = n/a.
PINNED_SCORES: dict[str, tuple[tuple[str | None, ...], bool]] = {
    "apex_bypass": (("1.0000", "0.0000", "1.0000", "1.0000", "0.7500", "1.0000"), False),
    "apex_neighbor": (("1.0000", "0.0000", "1.0000", "1.0000", "1.0000", "1.0000"), False),
    # round-2 finding #8: refs 0.3333 -> 0.2500 (directional_cites_refs now
    # also fails on the refs-less directional shape; strictly stricter)
    "buy_without_evidence": (("1.0000", "1.0000", "0.9524", None, "1.0000", "0.2500"), False),
    "dangling_refs": (("1.0000", "1.0000", "0.7500", None, "1.0000", "0.5000"), False),
    "echo_chamber": (("1.0000", "1.0000", "0.9048", "0.0000", "1.0000", "1.0000"), False),
    "echo_chamber_interleaved": (("1.0000", "1.0000", "0.9048", "0.0000", "1.0000", "1.0000"), False),
    "echo_chamber_padded": (("1.0000", "1.0000", "0.9048", "0.0000", "1.0000", "1.0000"), False),
    # round-4, round-3 finding #2: the clause-swapped reordering of the
    # interleaved echo PASSES at 0.6923 — a KNOWN false-PASS pinned to keep
    # the reorder leak visible (see the fixture description); the LCS
    # containment (0.2308 < 1/3) only ever certified order-preserving copies.
    "echo_chamber_reordered": (("1.0000", "1.0000", "0.9048", "0.6923", "1.0000", "1.0000"), True),
    "good_buy": (("1.0000", "1.0000", "0.8276", "0.9298", "1.0000", "1.0000"), True),
    "good_reject": (("1.0000", "1.0000", "0.8800", "0.8197", "1.0000", "1.0000"), True),
    "house_terms_debate": (("1.0000", "1.0000", "0.8800", "0.7397", "1.0000", "1.0000"), True),
    "insufficient_evidence": (("1.0000", None, None, None, "1.0000", "1.0000"), True),
    "insufficient_evidence_low": (("1.0000", None, None, None, "1.0000", "1.0000"), True),
    "missing_dissent": (("1.0000", "1.0000", "0.0000", None, "1.0000", "1.0000"), False),
    "mood_conviction": (("1.0000", "1.0000", "0.9565", None, "0.7500", "1.0000"), False),
    "mood_conviction_thesaurus": (("1.0000", "1.0000", "0.9565", None, "0.7500", "1.0000"), False),
    "pre_0013_evidence": ((None, "1.0000", "0.8947", None, "1.0000", None), True),
    "ungrounded_numbers": (("0.6667", "1.0000", "0.7647", None, "1.0000", "1.0000"), False),
    "unobservable_kills": (("1.0000", "0.0000", "0.8800", None, "1.0000", "1.0000"), False),
    "vacuous_dissent": (("1.0000", "1.0000", "0.0000", None, "1.0000", "1.0000"), False),
}


def test_fixture_files_are_frozen():
    """The corpus is code: any edit must break this pin (reviewed change)."""
    actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(FIXTURES_DIR.glob("*.json"))}
    assert actual == FIXTURE_SHA256


def test_thresholds_are_pinned():
    assert {n: str(t) for n, t in THRESHOLDS.items()} == PINNED_THRESHOLDS


def test_gate_constants_are_pinned():
    """Finding #20: the auxiliary constants are gates too — bumping one must
    break a pin, not ship green."""
    actual = {
        "HIGH_CONVICTION_HEDGE_MAX": HIGH_CONVICTION_HEDGE_MAX,
        "MIN_DISSENT_CONTENT_TOKENS": MIN_DISSENT_CONTENT_TOKENS,
        "MIN_DISSENT_NOVEL_TOKENS": MIN_DISSENT_NOVEL_TOKENS,
        "MIN_KILL_CRITERIA_DIRECTIONAL": MIN_KILL_CRITERIA_DIRECTIONAL,
        "MAX_SHARED_DEBATE_BIGRAMS": MAX_SHARED_DEBATE_BIGRAMS,
        "MAX_DEBATE_SEQ_CONTAINMENT": str(MAX_DEBATE_SEQ_CONTAINMENT),
        "MIN_DEBATE_NOVEL_TOKENS_PER_SIDE": MIN_DEBATE_NOVEL_TOKENS_PER_SIDE,
        "MIN_CONTENT_TOKEN_LEN": MIN_CONTENT_TOKEN_LEN,
        "STEM_MIN_KEEP": STEM_MIN_KEEP,
    }
    assert actual == PINNED_GATE_CONSTANTS
    assert CONVICTION_LABELS == PINNED_CONVICTION_LABELS


def test_lexicons_are_pinned_by_content():
    """Finding #21: a lexicon IS the metric definition. Sets are hashed over
    their sorted members, tuples in order, regexes over the pattern source —
    any pruning, fattening or reordering is a reviewed, pin-breaking diff."""
    def sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()
    actual = {
        "_HEDGES": sha("\n".join(sorted(metrics_mod._HEDGES))),
        "_HEDGE_PHRASES": sha("\n".join(metrics_mod._HEDGE_PHRASES)),
        "_STOPWORDS": sha("\n".join(sorted(metrics_mod._STOPWORDS))),
        "_STEM_SUFFIXES": sha("\n".join(metrics_mod._STEM_SUFFIXES)),
        "_MEASURABLE": sha(metrics_mod._MEASURABLE.pattern),
        "_TRIGGER": sha(metrics_mod._TRIGGER.pattern),
        "_NUM_COMPARATOR": sha(metrics_mod._NUM_COMPARATOR.pattern),
        "_VIBES_LEXEMES": sha("\n".join(sorted(metrics_mod._VIBES_LEXEMES))),
        "_VIBES_STREET": sha(metrics_mod._VIBES_STREET.pattern),
        "_ANCHOR_POSITIONAL": sha(metrics_mod._ANCHOR_POSITIONAL.pattern),
        "_TOKEN": sha(metrics_mod._TOKEN.pattern),
        "_EXEC_NUMBER": sha(_EXEC_NUMBER.pattern),
    }
    assert actual == PINNED_LEXICON_SHA256
    # round-4 (round-3 finding #7): the NA-marker list is deleted, not just
    # unpinned — a resurrected marker path must be a reviewed diff here.
    assert not hasattr(metrics_mod, "_DISSENT_NA_MARKERS")
    assert not hasattr(metrics_mod, "_VIBES_SUBJECTS")


def test_corpus_scores_are_golden_pinned():
    corpus = load_corpus()
    assert [b.bundle_id for b in corpus] == sorted(PINNED_SCORES)
    for bundle in corpus:
        s = score_bundle(bundle)
        got = tuple(None if r.score is None else str(r.score) for r in s.results)
        want_scores, want_overall = PINNED_SCORES[bundle.bundle_id]
        assert got == want_scores, f"{bundle.bundle_id}: {got}"
        assert s.passed is want_overall, bundle.bundle_id


def test_every_fixture_verdict_matches_its_pinned_expectations():
    _, mismatches = run_corpus()
    assert mismatches == []


def test_every_fixture_pins_every_metric_expectation():
    """A fixture with a missing expectation key is an unfrozen fixture."""
    keys = set(PINNED_THRESHOLDS) | {"overall"}
    for bundle in load_corpus():
        assert set(bundle.expected) == keys, bundle.bundle_id
        assert bundle.description, f"{bundle.bundle_id} must say why it exists"


def test_apex_bypass_fails_multiple_metrics():
    """Finding #6 (critical): the reviewer's all-axes-wrong memo must never
    again sweep the metrics. It fails at least two independently (kill
    observability and conviction conformance) and fails overall; the axes it
    still passes are the documented inherited/v1 limits, pinned as such in
    the fixture description."""
    bundle = next(b for b in load_corpus() if b.bundle_id == "apex_bypass")
    s = score_bundle(bundle)
    failed = {r.name for r in s.results if r.passed is False}
    assert {"kill_observability", "conviction_conformance"} <= failed
    assert s.passed is False


def test_apex_neighbor_composite_fails_the_anchor_rule():
    """Round-2 finding #2 + round-3 finding #1: the panel's rebuilt composite
    (hedge-free thesis, in-lexicon vibes kills, documented-limit dissent/
    debate) swept all six metrics at 1.0000 — twice: first with noun-smuggled
    vibes subjects, then again by trigger-fronting the round-2 leading-subject
    guard out of existence. The fixture now carries BOTH generations of
    bypass criteria and the anchor rule must fail every one of them (score
    0.0000, the composite's only non-documented leg) and the bundle overall.
    """
    bundle = next(b for b in load_corpus() if b.bundle_id == "apex_neighbor")
    assert len(bundle.kill_criteria) == 5     # 2 round-2 + 3 round-3 bypasses
    s = score_bundle(bundle)
    by_name = {r.name: r for r in s.results}
    assert by_name["kill_observability"].passed is False
    assert by_name["kill_observability"].score == Decimal("0.0000")
    assert s.passed is False


def test_echo_chamber_reordered_is_a_pinned_visible_leak():
    """Round-3 finding #2, rescoped round-4 (see the threat model in
    __init__.py): the clause-swapped reordering of the pinned interleaved
    echo defeats the order-assuming containment check and PASSES. v1 pins
    the leak VISIBLY instead of stacking more word-order machinery — same
    pattern as the out-of-lexicon vibes leak. If this test fails because the
    bundle started FAILING, a debate check got stricter: re-calibrate
    against the genuine-debate corpus and re-pin deliberately."""
    bundle = next(b for b in load_corpus()
                  if b.bundle_id == "echo_chamber_reordered")
    assert "KNOWN BYPASS" in bundle.description
    s = score_bundle(bundle)
    by_name = {r.name: r for r in s.results}
    assert by_name["debate_diversity"].passed is True    # the documented leak
    assert s.passed is True
    # ... and the order-preserving parent it was rewritten from still FAILs,
    # so the pair pins the exact boundary of what containment certifies
    parent = next(b for b in load_corpus()
                  if b.bundle_id == "echo_chamber_interleaved")
    assert score_bundle(parent).passed is False


def test_runner_corpus_mode_exits_zero_and_reports(capsys, tmp_path):
    out_json = tmp_path / "report.json"
    assert main(["--json", str(out_json)]) == 0
    out = capsys.readouterr().out
    assert "all corpus verdicts match pinned expectations" in out
    # every report carries the documented-limit caveats and the pre-0013
    # not-scoreable count (findings #12/#14/#15/#16)
    assert "known v1 limits" in out
    assert "not scoreable (pre-0013 evidence shape" in out
    report = json.loads(out_json.read_text())
    assert report["thresholds"] == PINNED_THRESHOLDS
    assert report["not_scoreable_pre_0013"] == 1      # pre_0013_evidence
    # 5 caveats: round-2 finding #1 added the kill-observability false-PASS
    # direction (out-of-lexicon vibes words); round-4 rescoped the lexical-
    # novelty caveat to name the reordered-echo blindness (round-3 finding
    # #2) so every rendered report states the debate metric is lexical and
    # order-sensitive
    assert len(report["known_v1_limits"]) == 5
    assert any("reordered" in c.lower() for c in report["known_v1_limits"])
    assert len(report["bundles"]) == len(PINNED_SCORES)
    by_id = {b["bundle_id"]: b for b in report["bundles"]}
    assert by_id["good_reject"]["passed"] is True
    assert by_id["ungrounded_numbers"]["passed"] is False
    assert by_id["apex_bypass"]["passed"] is False
    assert by_id["apex_neighbor"]["passed"] is False
    assert by_id["echo_chamber_interleaved"]["passed"] is False
    assert by_id["echo_chamber_reordered"]["passed"] is True   # visible leak
    assert by_id["house_terms_debate"]["passed"] is True
    assert by_id["insufficient_evidence_low"]["passed"] is True
    assert by_id["pre_0013_evidence"]["passed"] is True
