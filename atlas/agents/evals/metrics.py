"""Deterministic memo-quality metrics (desk-review 2026-07 item 8, v1).

Every metric is a pure function MemoBundle -> MetricResult. Scores are exact
Decimals quantized to 4dp (never floats); a score of None means the metric is
NOT APPLICABLE to that bundle (e.g. debate diversity on a memo with no
persisted debate) and neither passes nor fails. A bundle passes overall when
no applicable metric fails.

THRESHOLDS ARE PINNED CODE. Each threshold below documents WHY it sits where
it does; the corpus gate test golden-pins every fixture score, so moving a
threshold (or a lexicon) is a reviewed diff that must break pins — the same
never-weaken-a-gate discipline as the quant gates. The auxiliary gate
constants (HIGH_CONVICTION_HEDGE_MAX and friends) and every lexicon are
content-pinned in tests/unit/test_memo_eval_corpus.py: pruning a lexicon or
bumping a constant trips a pin even when no fixture sits on the boundary.

LEXICONS ARE CLOSED AND PINNED. The observability/trigger/hedge vocabularies
are deliberately small closed lists: a deterministic judge with a fuzzy
vocabulary is a vibes judge with extra steps. A memo phrased outside the
lexicon fails observability — and the fix is a more observable kill
criterion, never a fatter lexicon (the grounding verifier's rule: better
evidence, not a weaker verifier).

ABSENCE SEMANTICS (unified, eval-harness review 2026-07 findings #13/#17):
--db mode scores persisted rows that may predate today's schema gates, so the
judge re-checks what the cage would reject instead of assuming the cage ran.
On a DIRECTIONAL memo (recommendation != INSUFFICIENT_EVIDENCE) an absent
requirement is a FAIL, never n/a: a blank dissent fails dissent_distinctness,
fewer than two kill criteria fails kill_observability, an out-of-rubric
conviction label fails conviction_conformance, and a BUY without refs or
evidence fails refs_completeness (Constitution 4/5 re-checked). n/a is
reserved for INSUFFICIENT_EVIDENCE memos (the honest abstention) and for
provenance that structurally cannot exist (no 0019 debate rows; pre-0013
evidence bodies — see run_attached_evidence in bundles.py). Round-4 (round-3
finding #7): on an abstention the dissent column is never graded AT ALL —
blank, "N/A", "No dissent." or explanatory prose all score n/a, because an
abstention asserts no direction for a dissent to argue against (the schema
requires a dissent only on directional memos, schemas/memo.py).

KNOWN v1 LIMITS (documented, not silently accepted — eval-harness review
2026-07 findings #12/#14/#15 and the dissent/debate paraphrase limits):

* Grounding limits are INHERITED VERBATIM from the production cage BY DESIGN
  (atlas/agents/runtime/grounding.py): spelled-out word-numbers assert no
  numeric claim ("fourteen percent" grounds vacuously), ISO-date components
  in evidence ground small integers ("2026-07-10" grounds a bare "10"), and
  grounding is presence-not-attribution (a number that exists anywhere in the
  cited corpus is grounded even when the sentence attributes it to the wrong
  subject). The judge must never diverge from the cage, so these are NOT
  patched here; hardening belongs in grounding.py — a cage change, reviewed
  separately — so judge and cage tighten together.
* Rebuttal capitulation is invisible: debate_diversity reads the two OPENING
  cases only (rebuttals quote the opposing case by design), so a rebuttal
  that fully capitulates ("the bull is simply right") produces no signal.
  A stance-retention metric over rebuttals is deferred to v2.
* Closed lexicons under-credit genuinely observable events phrased outside
  them ("the position is stopped out", "the name is delisted") — the pinned
  trade-off is false FAILs over a fuzzy vocabulary; widening a lexicon is a
  reviewed, pin-breaking diff.
* Observability can still be OVER-credited (round-2 finding #1, the residual
  false-PASS direction): a pure-vibes criterion whose mood word is OUTSIDE
  the pinned vibes lexicon ("optimism on the name misses the benchmark",
  "hype around earnings turns sour") passes whenever the sentence happens to
  carry a measurable-lexicon noun plus a trigger verb. The vibes anchor rule
  below (round-4, replacing the round-2 leading-subject guard the round-3
  panel defeated by trigger-fronting) demands a HARD ANCHOR — a relational
  numeric threshold or a positional comparison of a measurable quantity —
  whenever a pinned vibes lexeme appears anywhere in the criterion. Word
  order carries no weight (parsing grammatical subjects is beyond a lexical
  judge), so a kill_observability PASS certifies lexicon conformance, not
  falsifiability.
* Purely LEXICAL novelty cannot grade meaning: a dissent of topical risk
  boilerplate that engages nothing scores as novel, and a debate side that
  paraphrases the other scores as diverse. The bigram-echo, ordered-
  containment and novel-floor checks below catch verbatim, padded and
  ORDER-PRESERVING interleaved echoes — the containment check assumes the
  copier keeps the copied stems in order. REORDERED partial-echo laundering
  (round-3 finding #2: clause-swap the copied points over the same
  interleaved filler) and semantic/thesaurus echoes are documented v1
  limits owned by the deferred LLM-judge half of item 8 (to be introduced
  under the same shadow-mode discipline as ADR-0005 model changes before it
  gates anything). echo_chamber_reordered.json pins the reorder leak
  VISIBLY as an expected PASS, the same pattern as the out-of-lexicon vibes
  leak test — v1 does not escalate lexical word-order machinery against a
  deliberately adversarial author (see the threat model in __init__.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from pydantic import BaseModel

from atlas.agents.evals.bundles import MemoBundle
from atlas.agents.runtime.grounding import grounding_violations, numeric_tokens
from atlas.agents.schemas.memo import _EXEC_NUMBER

_QUANT = Decimal("0.0001")


def _dec(f: Fraction) -> Decimal:
    """Exact rational -> 4dp Decimal (ROUND_HALF_EVEN default)."""
    return (Decimal(f.numerator) / Decimal(f.denominator)).quantize(_QUANT)


# ---------------------------------------------------------------------------
# Shared content tokenizer (overlap metrics only).
#
# Alphabetic tokens of three letters or more, minus a pinned stopword list.
# Digit-bearing tokens are EXCLUDED WHOLE (finding #3): "sma50" contributes
# nothing — not an "sma" residue — because a thesis citing SMA50 and a dissent
# citing SMA200 share evidence notation, not argument vocabulary; the
# grounding verifier owns numbers. Overlap is then compared at STEM level via
# the pinned mini-stemmer below (finding #11): "reject"/"rejecting"/
# "rejection" are the same lexeme, so an inflection-shifted restatement can
# no longer read as novel vocabulary. Stemming only ever GROWS the measured
# overlap relative to the old raw-token comparison, so this change is
# monotonically stricter — never a weakened gate.
_TOKEN = re.compile(r"[a-z0-9]+")
# Both tokenizer bounds are metric semantics (round-2 finding #9: everything
# that defines a metric is pinned, including the token regex above): tokens
# shorter than MIN_CONTENT_TOKEN_LEN are noise, and the stemmer never strips
# a suffix that would leave fewer than STEM_MIN_KEEP characters.
MIN_CONTENT_TOKEN_LEN = 3
STEM_MIN_KEEP = 4
_STOPWORDS = frozenset("""
a an the and or but nor of to in on for with at by from as is are was were be
been being it its this that these those not no nothing none we our ours they
their them him her his she he you your yours i me my us than then there here
if would could should will shall can has have had do does did done so such
over under into out up down about more most less least very still also both
each per any all some own same other another against between through during
before after above below because while when where which who whom whose what
how why yet even ever never always only just too again once
""".split())

# Pinned suffix-strip mini-stemmer: longest listed suffix is stripped
# repeatedly while at least four characters of stem remain. Deliberately
# tiny and closed — it exists to collapse ordinary English inflection
# (rejects/rejecting/rejection -> reject, strategy/strategies -> strateg),
# not to be a linguistics library. Both sides of every comparison are
# stemmed identically, so a rare over-collapse ("plane" -> "plan") only
# biases overlap UP — the strict direction.
_STEM_SUFFIXES = ("ation", "ment", "ing", "ion", "ies", "es", "ed", "ly",
                  "s", "e", "y")


def _stem(word: str) -> str:
    changed = True
    while changed:
        changed = False
        for suffix in _STEM_SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= STEM_MIN_KEEP:
                word = word[: len(word) - len(suffix)]
                changed = True
                break
    return word


def _content_words(text: str) -> list[str]:
    """Ordered content tokens: alphabetic-only (digit-bearing tokens are
    dropped whole), >= MIN_CONTENT_TOKEN_LEN letters, stopwords removed."""
    return [w for w in _TOKEN.findall(text.lower())
            if w.isalpha() and len(w) >= MIN_CONTENT_TOKEN_LEN
            and w not in _STOPWORDS]


def content_tokens(text: str) -> frozenset[str]:
    return frozenset(_content_words(text))


def stem_tokens(text: str) -> frozenset[str]:
    """Stem-level content vocabulary — the unit of overlap comparison."""
    return frozenset(_stem(w) for w in _content_words(text))


def _stem_bigrams(text: str) -> frozenset[tuple[str, str]]:
    """Adjacent stem pairs within one text span (never across spans)."""
    stems = [_stem(w) for w in _content_words(text)]
    return frozenset(zip(stems, stems[1:], strict=False))


def _stem_sequence(points: tuple[str, ...]) -> list[str]:
    """Ordered stem sequence across a side's points (duplicates kept)."""
    out: list[str] = []
    for p in points:
        out.extend(_stem(w) for w in _content_words(p))
    return out


def _lcs_len(a: list[str], b: list[str]) -> int:
    """Longest common subsequence length — the maximum number of one side's
    stems that appear IN ORDER in the other side's sequence. Classic DP;
    debate sides are a few dozen stems, so O(len(a)*len(b)) is nothing."""
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[len(b)]


# ---------------------------------------------------------------------------
# Kill-criterion observability lexicons.
#
# A kill criterion is OBSERVABLE when it names a quantity the fund can read
# off DCP tables or vendor records AND states a triggering event or
# comparison for it. "Sentiment deteriorates" names neither; "the close falls
# below both moving averages" names both. Word-stem matching (\w* suffixes)
# keeps inflections in-lexicon without stemming machinery.
#
# Bare time-unit words (day/week/month/quarter/session) were REMOVED from the
# measurable lexicon (finding #9): a duration alone is not a measurable
# quantity — "sentiment turns sour within a week" is a mood with a deadline,
# not a falsifiable condition. Time words may still appear in criteria, they
# just no longer carry measurability by themselves ("quarterly report" still
# qualifies via report/revenue/earnings). Strictly a stricter lexicon.
# rating\w* ADDED round-4 (round-3 finding #4 calibration): a credit-rating
# action ("Moody's downgrades the name's credit rating") is a vendor-record
# event in standard equity-memo language — the trigger (downgrade\w*) was
# already in-lexicon while the quantity it acts on was not, and EODHD
# fundamentals (queue item 9) make ratings a near-term readable record. A
# deliberate, reviewed widening: the content pin and this comment are the
# review trail.
_MEASURABLE = re.compile(
    r"\b(?:"
    r"close[sd]?|closing|price[sd]?|sma\w*|average\w*|return\w*|drawdown\w*|"
    r"volume\w*|earnings|revenue\w*|margin\w*|guidance|eps|dividend\w*|"
    r"gate\w*|verdict\w*|decile\w*|rebalance\w*|rating\w*|"
    r"report\w*|filing\w*|split\w*|benchmark\w*|spy|index"
    r")\b")
_TRIGGER = re.compile(
    r"\b(?:"
    r"below|above|under|over|beneath|"
    r"fall\w*|fell|drop\w*|declin\w*|ris(?:e|es|ing)|rose|break\w*|broke|"
    r"cross\w*|fail\w*|miss\w*|los\w*|exceed\w*|cut\w*|withdraw\w*|withdrew|"
    r"downgrade\w*|suspend\w*|halt\w*|expir\w*|enter\w*|exit\w*|"
    r"remain\w*|stay\w*|kept|keep\w*|turn\w*|revok\w*|stand\w*"
    r")\b")
# A bare numeric token only counts as measurable when the criterion states a
# RELATIONAL comparator for it (finding #9): "drops below rank 10" is a
# threshold, "over the next 20 sessions" is a deadline on a vibe. "over" and
# "under" are deliberately absent here (temporal readings — "over the next 20
# sessions" — would smuggle durations back in); they remain in _TRIGGER where
# they accompany a NAMED quantity ("closes over the SMA50").
_NUM_COMPARATOR = re.compile(
    r"\b(?:"
    r"below|above|beneath|exceed\w*|cross\w*|"
    r"at least|at most|more than|less than|greater than|fewer than"
    r")\b")

# Vibes anchor rule (round-4; round-3 findings #1/#3/#4 replaced the round-2
# leading-subject guard). The round-2 guard scanned only tokens before the
# FIRST trigger match — one lexical position — and the round-3 panel showed
# that position proves nothing in either direction: trigger-fronting
# ("Falling sentiment ... misses the vibe benchmark") emptied the window and
# resurrected the apex-neighbor sweep with the lexicon's own members
# (false-PASS), while narrative framing ("The bullish narrative breaks down
# if the close falls below both SMAs") put a vibes word before an idiomatic
# trigger and failed the house's own canonical observable condition
# (false-FAIL, flipping good_buy). The replacement takes word order out of
# the metric entirely:
#
#   a criterion containing ANY pinned vibes lexeme, anywhere, is observable
#   ONLY if it also states a HARD ANCHOR — a relational numeric threshold
#   (numeric token + _NUM_COMPARATOR) or a positional comparison of a
#   measurable-lexicon quantity (_ANCHOR_POSITIONAL + _MEASURABLE).
#
# "Sentiment index drops below 40" and "The bullish narrative breaks down if
# the close falls below both SMAs" pass (a hard relational comparison of a
# measurable quantity is mechanically checkable whatever series the sentence
# also mentions — the CIO template asks for "observable conditions under
# which the thesis is wrong", and both ARE); "Falling sentiment on the name
# misses the vibe benchmark" and "The benchmark misses the prevailing mood
# on the street" fail however the clauses are ordered, because miss/turn/
# sour state no comparison anchor. Calibration: every round-3 bypass and
# false-FAIL probe, pinned in tests and in the apex_neighbor fixture.
#
# The lexeme list is SURFACE FORMS with explicitly enumerated inflections
# and NO stem folding (round-3 finding #4: the mini-stemmer folded
# moody's -> mood and store/stores -> stor = stem of "story", failing
# classic observable criteria like same-store sales vs guidance). "street"
# is a vibes lexeme only as the idiom "the street" (the crowd); bare
# "street" ("Street consensus EPS estimates") stays out. A mood word
# phrased outside this closed lexicon ("optimism", "hype") still passes —
# the documented false-PASS direction in the module header, pinned visibly
# in tests and in the report caveats.
_VIBES_LEXEMES = frozenset({
    "sentiment", "sentiments", "mood", "moods", "narrative", "narratives",
    "story", "stories", "vibe", "vibes", "tone", "buzz", "chatter",
    "enthusiasm",
})
_VIBES_STREET = re.compile(r"\bthe\s+street\b")
# Strong positional comparators for the anchor rule — pinned surface forms.
# over/under are deliberately included HERE (unlike _NUM_COMPARATOR, where
# their temporal readings would smuggle durations into bare-number
# measurability): the positional anchor additionally requires a
# measurable-lexicon noun, and under the v1 threat model (accidental drift,
# see __init__.py — not an adversarial author) "over/under + measurable"
# is a comparison, not a deadline.
_ANCHOR_POSITIONAL = re.compile(
    r"\b(?:below|above|under|over|breach|breaches|crosses|exceeds)\b")


def criterion_observable(criterion: str) -> bool:
    low = criterion.lower()
    if _TRIGGER.search(low) is None:
        return False
    numeric_anchor = (bool(numeric_tokens(criterion))
                      and bool(_NUM_COMPARATOR.search(low)))
    if not (bool(_MEASURABLE.search(low)) or numeric_anchor):
        return False
    if any(t in _VIBES_LEXEMES for t in _TOKEN.findall(low)) \
            or _VIBES_STREET.search(low):
        # anchor rule: a vibes lexeme anywhere demands a hard anchor.
        return numeric_anchor or bool(_ANCHOR_POSITIONAL.search(low)
                                      and _MEASURABLE.search(low))
    return True


# ---------------------------------------------------------------------------
# Conviction-rubric hedge lexicon (cio/committee_memo.md rubric: HIGH means
# "the evidence would have to be materially wrong ... defend it against the
# dissent unprompted"). A HIGH-conviction thesis saturated with hedge words
# is a mood label stapled to an unsure argument. Closed, pinned list of
# SURFACE FORMS with NO stem folding (round-4; round-3 findings #5/#6
# replaced the round-2 stem fold): the mini-stemmer both under-reached the
# pinned lexemes' own families (-ity/-hood nouns are stemmer-unreachable, so
# "possibility ... likelihood ... probability" counted ZERO on a HIGH
# thesis — false-PASS) and over-reached into confident prose
# (potentially -> potential made the plain noun "upside potential" a hedge —
# false-FAIL undocumented by the round-2 collision list). Enumerating every
# counted inflection makes lexicon membership EXACTLY what the content pin
# hashes: "potential", "apparent", "hopeful", "like", "look", "looking"
# ("forward-looking guidance") and "rough" can never count; "potentially",
# "possibility", "likelihood" always do. Per pinned lexeme the enumeration
# covers the modal/adverb, adjective, and -ity/-hood noun (with plural)
# forms an LLM emits in memo prose. Unlike the observability lexicons this
# one FAILS OPEN — an out-of-lexicon hedge form counts 0 — so the pinned
# trade-off runs the permissive direction and thinness weakens the gate;
# widening stays a reviewed, pin-breaking diff. Calibration on the frozen
# corpus + round-3 probes: mood_conviction still counts 5, thesaurus 5,
# the round-2 panel's inflected thesis still 8, the round-3 derivational
# thesis (possibility/likelihood/probability) 0 -> 3 FAIL, the confident
# noun-'potential'-twice thesis 2 -> 0 PASS, and every genuine HIGH thesis
# (good_buy) still counts 0.
_HEDGES = frozenset({
    "may", "might", "could", "maybe", "perhaps",
    "possibly", "possible", "possibility", "possibilities",
    "probably", "probable", "probability", "probabilities",
    "likely", "likelihood",
    "potentially",
    "plausibly", "plausible", "plausibility",
    "conceivably", "conceivable",
    "presumably",
    "tentatively", "tentative",
    "somewhat", "roughly",
    "appears", "appear", "appeared", "apparently",
    "seems", "seem", "seemed", "seemingly",
    "suggests", "suggest", "suggested", "suggesting",
    "looks",
    "unclear", "uncertain", "uncertainty", "uncertainties",
    "arguably", "arguable", "borderline", "hopefully",
})
# Negated-certainty hedges are phrases, not words (finding #10); matched as
# pinned substrings of the lowercased thesis.
_HEDGE_PHRASES = ("not certain", "not clear", "hard to say", "hard to know",
                  "remains to be seen")
# One hedge is ordinary prose; two or more is a hedged thesis. Pinned.
HIGH_CONVICTION_HEDGE_MAX = 1

# The conviction rubric is a closed enum (schemas/memo.py Literal); a blank
# or out-of-enum label on any memo is nonconforming (finding #22 — the cage
# rejects it, and NULL-conviction legacy rows reach --db mode as "").
CONVICTION_LABELS = frozenset({"LOW", "MEDIUM", "HIGH", "N/A"})

# A dissent with fewer content stems than this cannot state "the strongest
# genuine case against" anything — it is vacuous by absence. Pinned.
MIN_DISSENT_CONTENT_TOKENS = 5
# ... and at least this many of its stems must be its OWN vocabulary (not in
# the thesis): a short dissent can clear the 0.5 novelty fraction with two
# filler words; a genuine counter-case needs substance of its own. Pinned.
# Calibration (frozen corpus): genuine fixture dissents carry 13-24 novel
# stems, the verbatim restatement 1 — the floor of 5 sits in the gap with
# margin on both sides.
MIN_DISSENT_NOVEL_TOKENS = 5

# Constitution 5 requires two kill criteria on any directional memo; the
# judge may not be weaker than the cage (finding #17). Pinned.
MIN_KILL_CRITERIA_DIRECTIONAL = 2

# Debate hard checks (findings #7/#8 + round-2 findings #3/#5), applied to
# the OPENING strongest_points only. Calibration (frozen corpus, stem-level,
# strongest_points only):
#   shared bigrams   — counted AFTER excluding evidence-derived bigrams
#                      (round-2 finding #5): a bigram both of whose stems
#                      appear in the bundle's own evidence bodies is shared
#                      EVIDENCE vocabulary, not echo — the debate prompts
#                      order both sides to anchor to the same corpus and
#                      name figures by their evidence IDs, so hyphenated
#                      house terms (xsmom-pit, walk-forward folds,
#                      decision-grade PASS) hit both sides by construction.
#                      Calibration: the panel's genuine prompt-conformant
#                      house-terms debate shared 10 bigrams raw and FAILED;
#                      after exclusion it counts 3 (< cap 4) and passes,
#                      while echo_chamber/echo_chamber_padded drop 22 -> 18
#                      counted — still 4.5x the unchanged cap — and
#                      good_buy/good_reject drop 1 -> 0. The cap itself
#                      did NOT move. The exclusion cannot launder a copied
#                      case: an all-evidence-vocabulary copy still fails the
#                      novel-stem floor and the containment cap below.
#   containment      — round-2 finding #3: filler INTERLEAVED between copied
#                      words destroys every copied bigram (bigrams are
#                      adjacent pairs), laundering a 68%-verbatim echo past
#                      the bigram cap. Interleaving cannot reorder what it
#                      copies, so the copied side's stem SEQUENCE survives
#                      as an ordered subsequence: max over both directions of
#                      LCS(bull_seq, bear_seq)/len(own_seq). Calibration:
#                      genuine debates measure 0.0000-0.1622 (good_buy
#                      0.1034, good_reject 0.1622, house-terms 0.1224,
#                      thesaurus-paraphrase apex debate 0.0000); echoes
#                      measure 0.5306-0.9615 (echo_chamber 0.9615, padded
#                      0.5306, the panel's interleaved echo 0.6923). The cap
#                      of 1/3 sits in that gap with ~2x margin on both sides
#                      (worst genuine 0.1622 vs cap 0.3333 vs worst attack
#                      0.5306).
#   novel stems/side — genuine sides carry 22-29 novel stems each;
#                      echo_chamber's bull side carries 0 (the bear is a
#                      superset restatement). The floor of 8 fails one-sided
#                      and superset-echo debates while genuine 3-point cases
#                      clear it nearly three times over.
MAX_SHARED_DEBATE_BIGRAMS = 4
MAX_DEBATE_SEQ_CONTAINMENT = Fraction(1, 3)
MIN_DEBATE_NOVEL_TOKENS_PER_SIDE = 8


def hedge_count(text: str) -> int:
    """Hedge tokens counted as exact surface forms against the pinned
    enumerated lexicon (round-4, round-3 findings #5/#6 — see the _HEDGES
    comment): no stem folding in either direction, so a form counts if and
    only if it is pinned. Ordinary inflections are IN the lexicon by
    enumeration (suggested, seemed, possibility, likelihood); confident
    homographs of hedge stems (the noun "potential", "forward-looking")
    are OUT by construction."""
    low = text.lower()
    words = sum(1 for w in _TOKEN.findall(low) if w in _HEDGES)
    phrases = sum(low.count(p) for p in _HEDGE_PHRASES)
    return words + phrases


# ---------------------------------------------------------------------------
# Results and thresholds
@dataclass(frozen=True)
class MetricResult:
    name: str
    score: Decimal | None          # None = not applicable
    threshold: Decimal
    passed: bool | None            # score >= threshold; None when score is None
    detail: str


THRESHOLDS: dict[str, Decimal] = {
    # The production cage fails a run closed on ONE ungrounded number; the
    # pre-outcome judge must never be weaker than the verifier it reuses, so
    # anything short of fully grounded fails. (Constitution 3.1's ban on
    # execution-shaped numerals is re-checked here too — same rule set as
    # the cage, reused verbatim, never reimplemented.)
    "grounding": Decimal("1.0000"),
    # The CIO template requires kill criteria to be "observable conditions";
    # with only two required, a single vibes criterion halves the memo's real
    # falsifiability, so every criterion must qualify.
    "kill_observability": Decimal("1.0000"),
    # score = |dissent \ thesis| / |dissent| at stem level: at least half of
    # the dissent's substantive vocabulary must be its own (the definition now
    # matches this stated rationale — finding #1 replaced the old min-
    # normalized overlap coefficient, which failed genuine long dissents
    # against short theses). The scorecard grades dissent post-outcome as the
    # complement of the call (dcp/scorecard.py dissent_right); a dissent that
    # restates the thesis makes that grading circular. Fixture calibration:
    # genuine dissents score 0.7500-0.9565; the verbatim restatement's raw
    # fraction is 0.0400 (floored to 0.0000 by the novel-stem minimum) and
    # the reviewer's inflection-shifted restatement scores 0.3913 — the 0.5
    # floor sits in the gap with margin on both sides.
    "dissent_distinctness": Decimal("0.5000"),
    # score = 1 - jaccard(bull opening points, bear opening points) at stem
    # level, gated by the bigram-echo cap and per-side novel floor above.
    # weakest_opposing_point and concede are EXCLUDED (finding #4): both
    # fields quote/grant the opposing case by design — the same rationale
    # that excludes rebuttals. Both sides argue over the SAME evidence
    # corpus, so vocabulary overlap is expected; an anchored copy shares
    # nearly everything. Fixture calibration: genuine debates score
    # 0.9298/0.8197; the echo chamber's raw divergence is 0.1379 and its
    # hard checks (22 shared bigrams, 0 novel bull stems) floor it to
    # 0.0000 — 0.5 splits the gap and answers the anchoring question
    # migration 0019 was landed to ask.
    "debate_diversity": Decimal("0.5000"),
    # Rubric conformance is a checklist, and the rubric exists so conviction
    # can be calibrated against outcomes (scorecard item 5): one nonconforming
    # label poisons the calibration series, so all checks must pass.
    "conviction_conformance": Decimal("1.0000"),
    # Refs are the join keys for provenance and the grounding corpus; one
    # dangling ref silently shrinks the corpus (grounding.py maps a missing
    # ref to ""), so completeness is all-or-nothing. Constitution 4's BUY
    # gates are re-checked here (finding #19).
    "refs_completeness": Decimal("1.0000"),
}


def _result(name: str, score: Decimal | None, detail: str) -> MetricResult:
    threshold = THRESHOLDS[name]
    passed = None if score is None else score >= threshold
    return MetricResult(name=name, score=score, threshold=threshold,
                        passed=passed, detail=detail)


def _pre_0013_shape(bundle: MemoBundle) -> bool:
    """Refs persisted, evidence bodies absent, AND the run provably attached
    evidence (research.agent_runs.input_refs) — the honest pre-migration-0013
    shape (finding #16). Without the run_attached_evidence discriminator this
    would also match a memo that fabricated refs when the runtime attached
    nothing; those keep failing closed (dangling refs, ungrounded numbers)."""
    return (bool(bundle.evidence_refs) and not bundle.evidence
            and bundle.run_attached_evidence)


_PRE_0013_DETAIL = ("not scoreable: refs persisted but evidence bodies "
                    "predate migration 0013 (run provably attached evidence; "
                    "bodies are unreconstructible and never backfilled)")


# ---------------------------------------------------------------------------
# 1. Evidence grounding — REUSES the production verifier verbatim.
#
# INHERITED LIMITS (finding #12, documented by design — see module header):
# word-numbers, date-component grounding and cross-block substitution pass
# here exactly as they pass the production cage. Hardening belongs in
# atlas/agents/runtime/grounding.py (a cage change, reviewed separately);
# the judge never diverges from the cage in either direction.
class _MemoNarrative(BaseModel):
    """The memo's narrative surface, shaped so grounding_violations() reads it
    exactly as the runner reads a CommitteeMemo: evidence_refs selects the
    cited corpus and is itself never treated as a numeric claim."""
    thesis: str
    kill_criteria: list[str]
    dissent: str
    debate_summary: str
    evidence_refs: list[str]


def grounding_score(bundle: MemoBundle) -> MetricResult:
    narrative = (bundle.thesis, *bundle.kill_criteria, bundle.dissent,
                 bundle.debate_summary)
    # Constitution 3.1 re-checked (finding #19): execution-shaped numerals
    # (currency, percent, price-shaped decimals, target/stop/size/entry) are
    # forbidden in narrative whatever the evidence says. Same pinned regex as
    # the cage (schemas/memo.py), reused verbatim.
    exec_hits = [t[:60] for t in narrative if _EXEC_NUMBER.search(t)]
    if _pre_0013_shape(bundle):
        # Numeric grounding needs the evidence bodies (never persisted before
        # migration 0013) — not scoreable. The 3.1 shape check needs none,
        # so it still fails closed.
        if exec_hits:
            return _result("grounding", Decimal("0.0000"),
                           f"execution-shaped numeric content (Constitution "
                           f"3.1): {exec_hits[0]!r}")
        return _result("grounding", None, _PRE_0013_DETAIL)
    payload = _MemoNarrative(
        thesis=bundle.thesis, kill_criteria=list(bundle.kill_criteria),
        dissent=bundle.dissent, debate_summary=bundle.debate_summary,
        evidence_refs=list(bundle.evidence_refs))
    total = sum(len(numeric_tokens(t)) for t in narrative)
    if total == 0 and not exec_hits:
        return _result("grounding", Decimal("1.0000"),
                       "no numeric claims in narrative")
    violations = grounding_violations(payload, dict(bundle.evidence))
    violations += [f"execution-shaped numeric content (Constitution 3.1): "
                   f"{t!r}" for t in exec_hits]
    denom = max(total, 1)
    score = _dec(Fraction(max(denom - len(violations), 0), denom))
    detail = (f"{max(denom - len(violations), 0)}/{denom} numeric claims clean"
              + (f"; {'; '.join(violations[:3])}" if violations else ""))
    return _result("grounding", score, detail)


# ---------------------------------------------------------------------------
# 2. Kill-criteria observability
def kill_observability(bundle: MemoBundle) -> MetricResult:
    name = "kill_observability"
    directional = bundle.recommendation != "INSUFFICIENT_EVIDENCE"
    if directional and len(bundle.kill_criteria) < MIN_KILL_CRITERIA_DIRECTIONAL:
        # Constitution 5 requires two; absent falsifiability may not outrank
        # weak falsifiability (findings #13/#17) — the judge is never weaker
        # than the cage.
        return _result(name, Decimal("0.0000"),
                       f"{len(bundle.kill_criteria)} kill criteria on a "
                       f"directional memo (Constitution 5 requires "
                       f"{MIN_KILL_CRITERIA_DIRECTIONAL})")
    if not bundle.kill_criteria:
        return _result(name, None,
                       "INSUFFICIENT_EVIDENCE memo with no kill criteria "
                       "(permitted: nothing was asserted to falsify)")
    flags = [criterion_observable(k) for k in bundle.kill_criteria]
    score = _dec(Fraction(sum(flags), len(flags)))
    bad = [f"[{i}] {k[:60]!r}" for i, (k, ok)
           in enumerate(zip(bundle.kill_criteria, flags, strict=True)) if not ok]
    detail = (f"{sum(flags)}/{len(flags)} criteria observable"
              + (f"; unobservable: {', '.join(bad)}" if bad else ""))
    return _result(name, score, detail)


# ---------------------------------------------------------------------------
# 3. Dissent non-vacuousness
#
# score = |dissent \ thesis| / |dissent| over stem-level content vocabulary:
# the fraction of the dissent's substantive vocabulary that is its OWN
# (finding #1 — this is the definition the 0.5 threshold always claimed).
# Degenerate shapes are explicit (finding #5): a blank/thin dissent on a
# directional memo fails, and a directional memo whose THESIS has no content
# tokens fails too — a dissent cannot be "distinct from" an empty argument,
# and auto-passing would reward corrupt rows. v1 LIMIT (finding #11,
# documented in the module header): novelty is lexical, so topical
# boilerplate that engages nothing still scores as novel — the semantic half
# is the deferred LLM judge.
def dissent_distinctness(bundle: MemoBundle) -> MetricResult:
    name = "dissent_distinctness"
    # Round-4 (round-3 finding #7, replacing the round-2 NA-marker list): on
    # a NON-DIRECTIONAL memo the dissent column is never graded, whatever it
    # holds — blank, "N/A", "No dissent.", or explanatory prose. An
    # abstention asserts no direction, so there is nothing for a dissent to
    # argue against: the schema requires a dissent only on directional memos
    # (schemas/memo.py — "Constitution 5: dissent required" fires only when
    # recommendation != INSUFFICIENT_EVIDENCE) and the CIO template's
    # dissent line ("the strongest genuine case against your
    # recommendation") has no referent on an abstention. The round-2 fix
    # whole-string-matched five pinned NA markers and failed the same honest
    # abstention one inch outside the list ("No dissent.", "N/A - no
    # evidence to argue against."); grading nothing is what the pinned
    # rationale ("never punish a desk for declining to argue what it cannot
    # ground") always implied. Directional memos are unchanged below: a
    # blank, thin or restated dissent keeps failing.
    if bundle.recommendation == "INSUFFICIENT_EVIDENCE":
        return _result(name, None,
                       "INSUFFICIENT_EVIDENCE memo: an abstention asserts no "
                       "direction to dissent against, so the dissent column "
                       "(blank, marker or prose) is not graded")
    dissent = stem_tokens(bundle.dissent)
    if len(dissent) < MIN_DISSENT_CONTENT_TOKENS:
        return _result(name, Decimal("0.0000"),
                       f"dissent too thin to grade ({len(dissent)} content "
                       f"stems < {MIN_DISSENT_CONTENT_TOKENS})")
    thesis = stem_tokens(bundle.thesis)
    if not thesis:
        return _result(name, Decimal("0.0000"),
                       "degenerate: directional memo whose thesis has no "
                       "content tokens — nothing to dissent from")
    novel = dissent - thesis
    if len(novel) < MIN_DISSENT_NOVEL_TOKENS:
        return _result(name, Decimal("0.0000"),
                       f"only {len(novel)} novel content stems < "
                       f"{MIN_DISSENT_NOVEL_TOKENS} — the dissent has almost "
                       f"no vocabulary of its own")
    score = _dec(Fraction(len(novel), len(dissent)))
    return _result(name, score,
                   f"{len(novel)}/{len(dissent)} dissent stems are novel "
                   f"({len(thesis)} thesis stems)")


# ---------------------------------------------------------------------------
# 4. Debate diversity (the anchoring measurement migration 0019 unlocked)
#
# Measured over the two OPENING strongest_points ONLY (finding #4):
# weakest_opposing_point and concede quote/grant the opposing case by design
# (schemas/debate.py requires it), so folding them in deflates genuine
# debates for doing exactly what the constitution asks. Hard checks first
# (findings #7/#8 + round-2 #3/#5): a side with no content is a one-sided
# debate, not a diverse one; non-evidence shared bigrams betray verbatim
# echo whatever padding is bolted on (padding grows the union but cannot
# remove a shared bigram); ordered-subsequence containment betrays an
# ORDER-PRESERVING copy whatever filler is interleaved into it (insertion
# cannot reorder the copied stems); and each side must bring a minimum of
# its own vocabulary.
#
# SCOPE, stated honestly (round-4; round-3 finding #2): these checks catch
# verbatim, padded, and order-preserving interleaved echoes — the classes
# where the copier keeps the copied stems in order. A copier who REORDERS
# (clause-swaps each copied point over the same interleaved filler) drops
# the LCS below the containment cap while the filler alone clears the
# novel-stem floors: echo_chamber_reordered.json pins that laundering
# variant as a KNOWN, VISIBLE false-PASS. v1 does not stack further
# word-order machinery against it — the harness judges accidental drift,
# not adversarial authorship (threat model in __init__.py), and reordered
# partial echo joins semantic/thesaurus echo as the deferred LLM judge's
# class (shadow-mode first, the ADR-0005 discipline).
def debate_diversity(bundle: MemoBundle) -> MetricResult:
    name = "debate_diversity"
    if bundle.debate is None:
        return _result(name, None, "no persisted debate (memos predating "
                                   "migration 0019 have no rows; presented "
                                   "honestly, never backfilled)")
    bull_text = " ".join(bundle.debate.bull.strongest_points)
    bear_text = " ".join(bundle.debate.bear.strongest_points)
    bull = stem_tokens(bull_text)
    bear = stem_tokens(bear_text)
    if not bull or not bear:
        return _result(name, Decimal("0.0000"),
                       f"one-sided or degenerate debate: {len(bull)} bull / "
                       f"{len(bear)} bear content stems")
    shared = (
        frozenset().union(*(_stem_bigrams(p)
                            for p in bundle.debate.bull.strongest_points))
        & frozenset().union(*(_stem_bigrams(p)
                              for p in bundle.debate.bear.strongest_points)))
    # round-2 finding #5: bigrams whose BOTH stems live in the bundle's own
    # evidence bodies are the corpus both sides were ordered to argue from
    # (house terms), not echo. No evidence -> no exclusion (fail closed).
    evidence_stems: frozenset[str] = frozenset().union(
        *(stem_tokens(body) for _, body in bundle.evidence)) \
        if bundle.evidence else frozenset()
    echoed_bigrams = [bg for bg in shared
                      if not (bg[0] in evidence_stems
                              and bg[1] in evidence_stems)]
    if len(echoed_bigrams) > MAX_SHARED_DEBATE_BIGRAMS:
        return _result(name, Decimal("0.0000"),
                       f"echoed phrasing: {len(echoed_bigrams)} shared "
                       f"bigrams > {MAX_SHARED_DEBATE_BIGRAMS} after "
                       f"excluding {len(shared) - len(echoed_bigrams)} "
                       f"evidence-derived (padding cannot launder a copied "
                       f"case)")
    # round-2 finding #3: interleaved filler destroys copied bigrams but
    # cannot reorder the copied stems — the ordered subsequence survives.
    bull_seq = _stem_sequence(bundle.debate.bull.strongest_points)
    bear_seq = _stem_sequence(bundle.debate.bear.strongest_points)
    common = _lcs_len(bull_seq, bear_seq)
    containment = max(Fraction(common, len(bull_seq)),
                      Fraction(common, len(bear_seq)))
    if containment > MAX_DEBATE_SEQ_CONTAINMENT:
        return _result(name, Decimal("0.0000"),
                       f"interleaved echo: {_dec(containment)} of one side's "
                       f"stem sequence appears in order in the other's "
                       f"(cap {_dec(Fraction(MAX_DEBATE_SEQ_CONTAINMENT))}; "
                       f"filler cannot launder an order-preserving copy)")
    novel_bull, novel_bear = len(bull - bear), len(bear - bull)
    if min(novel_bull, novel_bear) < MIN_DEBATE_NOVEL_TOKENS_PER_SIDE:
        return _result(name, Decimal("0.0000"),
                       f"a side brings too little of its own vocabulary "
                       f"({novel_bull} bull / {novel_bear} bear novel stems, "
                       f"floor {MIN_DEBATE_NOVEL_TOKENS_PER_SIDE})")
    jaccard = Fraction(len(bull & bear), len(bull | bear))
    score = _dec(Fraction(1) - jaccard)
    return _result(name, score,
                   f"jaccard {_dec(jaccard)} over {len(bull)} bull / "
                   f"{len(bear)} bear stems (opening strongest_points only); "
                   f"{len(echoed_bigrams)} echoed bigrams "
                   f"({len(shared)} shared raw); containment "
                   f"{_dec(containment)}")


# ---------------------------------------------------------------------------
# 5. Conviction-rubric conformance
def conviction_conformance(bundle: MemoBundle) -> MetricResult:
    name = "conviction_conformance"
    failures: list[str] = []
    checks = 4
    # (a) the label must be in the rubric's closed enum (finding #22): a
    # blank (NULL column) or out-of-enum label is nonconforming on ANY memo —
    # the cage's Literal type rejects it, and a labelless row would otherwise
    # poison the conviction-calibration series (scorecard item 5).
    if bundle.conviction not in CONVICTION_LABELS:
        failures.append(f"label_out_of_rubric ({bundle.conviction!r})")
    # (b) Abstention vs conviction (round-2 finding #6 replaced the old
    # na_iff_insufficient XOR, which failed every prompt-conformant
    # abstention): the CIO template (prompts/cio/committee_memo.md line 10,
    # "conviction: LOW/MEDIUM/HIGH; cap at LOW when evidence_available=false")
    # never offers N/A, so a compliant model abstains with LOW — an
    # INSUFFICIENT_EVIDENCE memo conforms with conviction in {N/A, LOW}
    # (N/A stays valid: the schema enum allows it and fixtures/legacy rows
    # carry it). MEDIUM/HIGH on an abstention is still a broken rubric, and
    # N/A on a directional call still is too.
    if bundle.recommendation == "INSUFFICIENT_EVIDENCE":
        if bundle.conviction not in ("N/A", "LOW"):
            failures.append(f"ie_conviction_above_low ({bundle.conviction!r})")
    elif bundle.conviction == "N/A":
        failures.append("na_on_directional")
    # (c) no evidence corpus => conviction capped at LOW (mirrors the schema
    # gate; re-checked because --db mode scores rows that may predate it).
    # The pre-0013 shape is exempt: the run provably attached evidence, only
    # the bodies predate migration 0013 (finding #16).
    if (not bundle.evidence and not _pre_0013_shape(bundle)
            and bundle.conviction in ("MEDIUM", "HIGH")):
        failures.append("low_without_evidence")
    # (d) HIGH means defensible unprompted: a hedge-saturated thesis under a
    # HIGH label is mood, not conviction.
    if bundle.conviction == "HIGH":
        n = hedge_count(bundle.thesis)
        if n > HIGH_CONVICTION_HEDGE_MAX:
            failures.append(f"high_not_hedged ({n} hedge tokens > "
                            f"{HIGH_CONVICTION_HEDGE_MAX})")
    score = _dec(Fraction(checks - len(failures), checks))
    detail = ("conforms to the conviction rubric" if not failures
              else "failed: " + ", ".join(failures))
    return _result(name, score, detail)


# ---------------------------------------------------------------------------
# 6. evidence_refs completeness (+ Constitution 4 BUY gates re-checked)
def refs_completeness(bundle: MemoBundle) -> MetricResult:
    name = "refs_completeness"
    if _pre_0013_shape(bundle):
        # Refs cannot be resolved against bodies that were never persisted;
        # judging them dangling would accuse compliant historical memos of
        # fabrication (finding #16). Reported honestly, counted in the report.
        return _result(name, None, _PRE_0013_DETAIL)
    known = {r for r, _ in bundle.evidence}
    dangling = [r for r in bundle.evidence_refs if r not in known]
    checks: list[tuple[str, bool]] = [
        ("no_dangling_refs", not dangling)]
    if bundle.recommendation != "INSUFFICIENT_EVIDENCE":
        # the CIO template (prompts/cio/committee_memo.md lines 20-22): "list
        # every reference ID you relied on — a REJECT verdict still cites the
        # evidence that justified the rejection ... Empty list only for
        # INSUFFICIENT_EVIDENCE." Round-2 finding #8: this check was gated on
        # bundle.evidence being non-empty, so a directional verdict persisted
        # with zero evidence rows AND zero refs swept 1.0000 while its BUY
        # twin failed — an unaccountable verdict passed for pointing the safe
        # direction. The compliant shape for nothing-to-cite is
        # INSUFFICIENT_EVIDENCE, so every directional memo must cite. (The
        # pre-0013 shape returned n/a above, unchanged.)
        checks.append(("directional_cites_refs",
                       len(bundle.evidence_refs) > 0))
    if bundle.recommendation == "BUY":
        # Constitution 4 re-checked (finding #19): "BUY without evidence_refs
        # is forbidden" and "BUY forbidden when no DCP evidence attached" —
        # --db mode scores rows that may predate or bypass the cage.
        checks.append(("buy_cites_refs", len(bundle.evidence_refs) > 0))
        checks.append(("buy_has_dcp_evidence", len(bundle.evidence) > 0))
    passed = sum(1 for _, ok in checks if ok)
    score = _dec(Fraction(passed, len(checks)))
    failed = [n for n, ok in checks if not ok]
    detail = ("all cited refs resolve" if not failed
              else "failed: " + ", ".join(failed)
              + (f"; dangling: {dangling}" if dangling else ""))
    return _result(name, score, detail)


# ---------------------------------------------------------------------------
METRICS = (grounding_score, kill_observability, dissent_distinctness,
           debate_diversity, conviction_conformance, refs_completeness)


@dataclass(frozen=True)
class BundleScore:
    bundle_id: str
    symbol: str
    recommendation: str
    results: tuple[MetricResult, ...]

    @property
    def passed(self) -> bool:
        """No applicable metric failed. Not-applicable metrics (None) neither
        pass nor fail — a memo without a debate is not penalized for it."""
        return all(r.passed is not False for r in self.results)


def score_bundle(bundle: MemoBundle) -> BundleScore:
    return BundleScore(bundle_id=bundle.bundle_id, symbol=bundle.symbol,
                       recommendation=bundle.recommendation,
                       results=tuple(fn(bundle) for fn in METRICS))
