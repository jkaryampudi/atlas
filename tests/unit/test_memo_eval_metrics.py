"""Unit behavior of each memo-quality metric (desk-review 2026-07 item 8).

The corpus test pins whole-bundle scores; this file pins the EDGES of each
metric definition — the whitelists, the applicability (None) semantics, the
exact rule each threshold guards — so a lexicon or rule change is caught at
the definition, not just in aggregate. Boundary tests use LITERAL values
(never the constants they guard) so bumping a gate constant fails here even
before the corpus pins trip (eval-harness review 2026-07 finding #20).
"""
from __future__ import annotations

from decimal import Decimal

from atlas.agents.evals.bundles import DebateSideView, DebateView, MemoBundle
from atlas.agents.evals.metrics import (
    content_tokens,
    conviction_conformance,
    criterion_observable,
    debate_diversity,
    dissent_distinctness,
    grounding_score,
    hedge_count,
    kill_observability,
    refs_completeness,
    score_bundle,
    stem_tokens,
)

EVIDENCE = (
    ("dcp:bars:X:2026-07-10",
     "X daily closes: latest close 100.50 on 2026-07-10, 20 sessions ago 90.25. "
     "Window: 60 sessions."),
    ("quant:families:X:2026-07-10",
     "Quant record: momentum-v1 FAILED gates (0 of 4 folds passed)."),
)


def _bundle(**over) -> MemoBundle:
    base = dict(
        bundle_id="t", symbol="X", recommendation="REJECT", conviction="LOW",
        thesis="The committee rejects the name because no validated strategy covers it.",
        kill_criteria=("The close falls below the SMA50",
                       "The family verdict is revoked at a quarterly review"),
        evidence_refs=("dcp:bars:X:2026-07-10", "quant:families:X:2026-07-10"),
        dissent="Persistent relative strength argues the machinery will select "
                "this name shortly and waiting surrenders the entry.",
        debate_summary="", evidence=EVIDENCE, debate=None)
    base.update(over)
    return MemoBundle(**base)


# ---------------------------------------------------------------------------
# grounding (reuses the production verifier — these pin the reuse, not new logic)
def test_grounding_no_numerals_is_vacuously_grounded():
    r = grounding_score(_bundle())
    assert r.score == Decimal("1.0000") and r.passed is True


def test_grounding_verbatim_tokens_pass_and_fabricated_fail():
    good = _bundle(thesis="The record shows 0 of 4 folds passed for the family.")
    assert grounding_score(good).passed is True
    bad = _bundle(thesis="The record shows 3 of 4 folds passed for the family.")
    r = grounding_score(bad)
    assert r.passed is False and "'3'" in r.detail


def test_grounding_years_are_whitelisted_like_production():
    r = grounding_score(_bundle(thesis="Nothing since 2024 supports deployment."))
    assert r.passed is True


def test_grounding_identifier_digits_assert_no_claim():
    """'SMA50' is an identifier on the narrative side (production tokenizer)."""
    r = grounding_score(_bundle(thesis="Price sits below the SMA50 today."))
    assert r.score == Decimal("1.0000")


def test_grounding_corpus_is_cited_refs_only():
    """A number grounded only by an UNCITED block is still a fabrication —
    exactly the production rule (grounding.py selects by evidence_refs)."""
    r = grounding_score(_bundle(
        thesis="The record shows 0 of 4 folds passed for the family.",
        evidence_refs=("dcp:bars:X:2026-07-10",)))   # quant block not cited
    assert r.passed is False


def test_grounding_rechecks_execution_shaped_numbers():
    """Constitution 3.1 re-checked (finding #19): a target/stop/size/entry
    number is forbidden in narrative even when the token itself is grounded
    verbatim in cited evidence — same pinned regex as the cage."""
    r = grounding_score(_bundle(
        thesis="The desk sets a target of 20 for the name.",  # '20' IS in evidence
        ))
    assert r.passed is False and "Constitution 3.1" in r.detail


def test_grounding_pre_0013_shape_is_not_scoreable_not_a_fail():
    """Finding #16: refs persisted, bodies predate migration 0013, run
    provably attached evidence -> n/a, never a fabrication accusation."""
    r = grounding_score(_bundle(
        thesis="The record shows 0 of 4 folds passed for the family.",
        evidence=(), run_attached_evidence=True))
    assert r.score is None and r.passed is None
    assert "0013" in r.detail


def test_grounding_fabricated_refs_without_run_evidence_fail_closed():
    """Same persisted shape but the run attached NOTHING: the refs are the
    memo's own invention and numeric claims stay ungrounded (finding #16's
    discriminator — the honest n/a must not become a bypass)."""
    r = grounding_score(_bundle(
        thesis="The record shows 0 of 4 folds passed for the family.",
        evidence=(), run_attached_evidence=False))
    assert r.passed is False


def test_grounding_pre_0013_still_fails_execution_shapes():
    """The 3.1 shape check needs no evidence bodies, so it fails closed even
    on the pre-0013 shape."""
    r = grounding_score(_bundle(
        thesis="The desk sets a target of 20 for the name.",
        evidence=(), run_attached_evidence=True))
    assert r.score == Decimal("0.0000") and r.passed is False


# ---------------------------------------------------------------------------
# kill-criteria observability
def test_observable_needs_measurable_and_trigger():
    assert criterion_observable("The close falls below both moving averages")
    # trigger without measurable: a mood turning is not a quantity
    assert not criterion_observable("Investor mood turns negative on the story")
    # measurable without trigger: a quantity with no stated event
    assert not criterion_observable("The twenty-session return and the SMA50")
    # pure vibes: neither
    assert not criterion_observable("The thesis stops feeling durable")


def test_time_units_alone_are_not_measurable():
    """Finding #9: a duration is a deadline, not a quantity — the old lexicon
    passed every one of these pure-vibes criteria via day/week/month/session
    words plus a ubiquitous verb."""
    assert not criterion_observable(
        "Street sentiment on the name turns sour within a week")
    assert not criterion_observable(
        "Conviction in the story breaks down over the coming sessions")
    assert not criterion_observable(
        "Retail enthusiasm declines and the story loses its shine this month")
    # ... while genuinely observable criteria that HAPPEN to carry time words
    # still qualify via a named quantity:
    assert criterion_observable(
        "SPY closes below its SMA50 and stays below it for ten consecutive sessions")
    assert criterion_observable(
        "The next quarterly report shows revenue declining from the prior year")


def test_numeric_token_needs_a_relational_comparator():
    """Finding #9: an evidence-present digit is not measurability by itself —
    'over the next 20 sessions' is a deadline on a vibe; 'below rank 10' is
    a threshold."""
    assert not criterion_observable(
        "Sentiment drops materially over the next 20 sessions")
    assert criterion_observable(
        "The name drops below rank 10 at a monthly rebalance")


def test_vibes_lexeme_anywhere_demands_a_hard_anchor():
    """Round-4 anchor rule (round-3 findings #1/#3, replacing the round-2
    leading-subject guard): a pinned vibes lexeme ANYWHERE in the criterion
    makes it observable only with a hard anchor — a relational numeric
    threshold or a positional comparison of a measurable-lexicon quantity.
    Word order carries no weight, so the round-2 noun-smuggles and the
    round-3 trigger-fronted rewrites of them fail identically."""
    # round-2 noun-smuggles (measurable noun + trigger verb around a mood)
    assert not criterion_observable("The narrative around earnings turns sour")
    assert not criterion_observable(
        "Investor mood on the name misses the vibe benchmark")
    # round-3 finding #1 bypasses: fronting the trigger emptied the old
    # guard's window; a vibes word after the first trigger escaped it
    assert not criterion_observable(
        "Falling sentiment on the name misses the vibe benchmark")
    assert not criterion_observable(
        "Watch for turning sentiment against the earnings benchmark")
    assert not criterion_observable(
        "The benchmark misses the prevailing mood on the street")
    assert not criterion_observable(
        "Turning sour, the mood around earnings misses the benchmark")
    # explicit surface plurals are in the lexicon (no stem folding)
    assert not criterion_observable(
        "The stories around the earnings report turn negative")


def test_vibes_lexeme_with_hard_anchor_is_observable():
    """The anchor rule's PASS side (round-3 finding #3 false-FAILs restored):
    a hard relational comparison of a measurable quantity is mechanically
    checkable whatever series the sentence also names — the CIO template
    asks for 'observable conditions under which the thesis is wrong', and
    these are. The round-2 guard failed all of these on word order alone,
    flipping good_buy to OVERALL FAIL on high-frequency LLM phrasing."""
    # narrative framing around the house's canonical observable condition
    assert criterion_observable(
        "The bullish narrative breaks down if the close falls below both "
        "the SMA20 and the SMA50")
    # numeric anchor: an index level against a relational threshold
    assert criterion_observable("Sentiment index drops below 40")
    # positional anchor: a series compared against its own average —
    # deliberately OBSERVABLE under the round-4 rescope (the round-2 guard
    # pinned this False; a below-average condition on a vendor-readable
    # series is checkable, and word-order subject detection is exactly the
    # machinery round 3 defeated)
    assert criterion_observable(
        "Sentiment falls below its 20-session average")
    # vibes mentioned incidentally next to an anchored condition
    assert criterion_observable(
        "The close falls below the SMA50 while street chatter stays euphoric")
    # documented false-PASS direction (module header + report caveats): a
    # mood word OUTSIDE the closed lexicon needs no anchor at all — pinned
    # here so the limit stays visible, not silent
    assert criterion_observable("The optimism around earnings turns sour")


def test_no_stem_folding_collisions_in_the_vibes_lexicon():
    """Round-3 finding #4: the round-2 stem fold captured non-vibes words —
    moody's -> 'mood', store/stores -> 'stor' = the stem of 'story' — and
    failed classic vendor-record kill criteria. Surface forms cannot
    collide. (Moody's also needed rating\\w* in _MEASURABLE — the reviewed
    widening documented at the regex.)"""
    assert criterion_observable("Moody's downgrades the name's credit rating")
    assert criterion_observable(
        "Same-store sales fall below the company guidance in the next report")
    assert criterion_observable(
        "Store traffic falls below the reported average")
    # 'street' is a vibes lexeme only as the idiom 'the street'; bare Street
    # modifying a vendor-recorded measurable stays observable
    assert criterion_observable(
        "Street consensus EPS estimates fall below the prior guidance")


def test_kill_observability_scores_fraction_and_names_offenders():
    r = kill_observability(_bundle(kill_criteria=(
        "The close falls below the SMA50",
        "Sentiment deteriorates materially")))
    assert r.score == Decimal("0.5000") and r.passed is False
    assert "Sentiment deteriorates" in r.detail


def test_directional_memo_with_missing_kill_criteria_fails():
    """Findings #13/#17: Constitution 5 requires two kill criteria on a
    directional memo; zero (or one) is absent falsifiability and must FAIL —
    the judge is never weaker than the cage, and absence may not outrank a
    weak criterion. Boundary literals: 0 and 1 fail, 2 grade normally."""
    r0 = kill_observability(_bundle(kill_criteria=()))
    assert r0.score == Decimal("0.0000") and r0.passed is False
    r1 = kill_observability(_bundle(
        kill_criteria=("The close falls below the SMA50",)))
    assert r1.score == Decimal("0.0000") and r1.passed is False
    r2 = kill_observability(_bundle())
    assert r2.score == Decimal("1.0000") and r2.passed is True


def test_kill_criteria_not_applicable_only_for_insufficient_evidence():
    r = kill_observability(_bundle(recommendation="INSUFFICIENT_EVIDENCE",
                                   kill_criteria=()))
    assert r.score is None and r.passed is None


# ---------------------------------------------------------------------------
# dissent distinctness
def test_dissent_restatement_fails():
    thesis = ("The committee rejects the name because no validated strategy "
              "covers it and the momentum family failed its gates.")
    dissent = ("Arguably the committee rejects the name because no validated "
               "strategy covers it and the momentum family failed its gates.")
    r = dissent_distinctness(_bundle(thesis=thesis, dissent=dissent))
    assert r.passed is False


def test_dissent_inflection_shifted_restatement_fails():
    """Finding #11: morphological inflection is not novel vocabulary — the
    pinned mini-stemmer folds rejects/rejecting, strategy/strategies,
    validated/validation, deployment/deploying into one lexeme each."""
    thesis = ("The committee rejects AVGO because no validated strategy "
              "covers the name: momentum-v1 failed its decision gates on "
              "real data and AVGO sits outside the xsmom-pit winner decile, "
              "so trend strength alone cannot justify deployment.")
    dissent = ("Rejecting stays right: strategies lacking validation cannot "
               "cover names, momentum's gate failure on live figures means "
               "deploying is unjustified while the name sits outside winner "
               "deciles on real data.")
    r = dissent_distinctness(_bundle(thesis=thesis, dissent=dissent))
    assert r.passed is False


def test_dissent_genuine_long_dissent_against_short_thesis_passes():
    """Finding #1: the old min-normalized overlap coefficient scored this
    genuine dissent 0.0000 FAIL because engaging a six-token thesis naturally
    reuses all six tokens. Under |dissent \\ thesis| / |dissent| it passes:
    most of its vocabulary is its own."""
    r = dissent_distinctness(_bundle(
        dissent="The committee rejects a name its own validated machinery is "
                "close to selecting: no strategy covers it today, yet the "
                "point-in-time record shows persistent structure, relative "
                "strength has led every rebalance, and waiting surrenders "
                "the entry while coverage catches up."))
    assert r.passed is True and r.score >= Decimal("0.5000")


def test_dissent_thin_is_vacuous_by_absence():
    r = dissent_distinctness(_bundle(dissent="No real objection."))
    assert r.score == Decimal("0.0000") and r.passed is False
    assert "too thin" in r.detail


def test_dissent_novel_stem_floor_is_literal_five():
    """Finding #20 boundary, literal values: six content stems of which only
    four are novel -> FAIL on the novelty floor; a fifth novel stem with the
    fraction above half -> graded and passing."""
    thesis = "The committee rejects deployment because momentum failed badly."
    # stems: committe/reject/deploy/momentum/fail/bad
    four_novel = ("The committee rejects deployment: whipsaw, liquidity, "
                  "crowding, slippage.")
    r = dissent_distinctness(_bundle(thesis=thesis, dissent=four_novel))
    assert r.score == Decimal("0.0000") and r.passed is False
    assert "4 novel" in r.detail
    five_novel = ("The committee rejects deployment: whipsaw, liquidity, "
                  "crowding, slippage, correlation.")
    r = dissent_distinctness(_bundle(thesis=thesis, dissent=five_novel))
    assert r.passed is True


def test_dissent_blank_on_directional_memo_fails():
    """--db mode scores rows that may predate the schema gate."""
    assert dissent_distinctness(_bundle(dissent="")).passed is False


def test_dissent_empty_thesis_on_directional_memo_fails():
    """Finding #5: a blank (or all-stopword) thesis on a directional memo is
    a degenerate row — there is nothing to be distinct FROM, and the old
    division guard scored it a perfect 1.0000."""
    r = dissent_distinctness(_bundle(thesis=""))
    assert r.score == Decimal("0.0000") and r.passed is False
    r = dissent_distinctness(_bundle(thesis="It is so and we will do this."))
    assert r.score == Decimal("0.0000") and r.passed is False


def test_dissent_not_applicable_only_for_insufficient_evidence():
    r = dissent_distinctness(_bundle(
        recommendation="INSUFFICIENT_EVIDENCE", dissent=""))
    assert r.score is None and r.passed is None


def test_dissent_never_graded_on_an_abstention():
    """Round-4 (round-3 finding #7, replacing the round-2 five-string NA
    marker list): an INSUFFICIENT_EVIDENCE memo asserts no direction, so
    there is nothing for a dissent to argue against — the schema requires a
    dissent only on directional memos (schemas/memo.py) — and the dissent
    column is n/a WHATEVER it holds. The round-2 marker list failed the same
    honest abstention one inch outside it ('No dissent.', 'Nil.', 'N/A - no
    evidence to argue against.')."""
    for dissent in ("Not applicable.", "N/A", "n/a", "None.", "none",
                    "No dissent.", "Nil.",
                    "N/A - no evidence to argue against.",
                    # explanatory prose is not graded either: distinctness
                    # measures a counter-case against a direction, and an
                    # abstention states none
                    "Persistent relative strength argues the machinery will "
                    "select this name shortly and waiting surrenders the "
                    "entry."):
        r = dissent_distinctness(_bundle(
            recommendation="INSUFFICIENT_EVIDENCE", dissent=dissent))
        assert r.score is None and r.passed is None, dissent
    # ... and the whole-bundle verdict for the prompt-conformant abstention
    # phrasing that round 3 showed failing: n/a never fails the memo
    s = score_bundle(_bundle(
        recommendation="INSUFFICIENT_EVIDENCE", conviction="LOW",
        kill_criteria=(), evidence_refs=(), evidence=(),
        dissent="No dissent."))
    assert s.passed is True
    # directional memos unchanged: an NA marker or blank there is a thin
    # dissent / absent requirement and keeps FAILING
    r = dissent_distinctness(_bundle(dissent="Not applicable."))
    assert r.score == Decimal("0.0000") and r.passed is False
    assert "too thin" in r.detail
    r = dissent_distinctness(_bundle(dissent="No dissent."))
    assert r.score == Decimal("0.0000") and r.passed is False


def test_dissent_numbers_do_not_count_as_shared_vocabulary():
    """Digit-bearing tokens are excluded from overlap sets WHOLE (finding #3):
    'SMA50' contributes nothing — not an 'sma' residue — and pure numbers
    contribute nothing either."""
    assert "100" not in content_tokens("close of 100.50 on the window")
    # 'below'/'the'/'and' are stopwords; the identifiers contribute NOTHING
    assert content_tokens("below the SMA50 and SMA200") == frozenset()
    assert "sma" not in content_tokens("thesis cites SMA50 and SMA200 levels")


def test_stem_tokens_fold_inflections():
    assert stem_tokens("rejects rejecting rejection") == {"reject"}
    assert stem_tokens("strategy strategies") == {"strateg"}
    assert len(stem_tokens("deployment deploying deploys")) == 1


# ---------------------------------------------------------------------------
# debate diversity
def _side(*points: str, weakest: str = "The other side ignores the record",
          concede: str = "Coverage is absent today") -> DebateSideView:
    return DebateSideView(strongest_points=points,
                          weakest_opposing_point=weakest, concede=concede)


def test_debate_absent_is_not_applicable():
    r = debate_diversity(_bundle(debate=None))
    assert r.score is None and r.passed is None


def test_anchored_copy_fails_and_distinct_cases_pass():
    bull = _side("Persistent trend shows institutional accumulation",
                 "Relative strength argues real demand",
                 "Validated family proves momentum edges exist")
    echo = _side("Persistent trend shows institutional accumulation clearly",
                 "Relative strength argues real demand indeed",
                 "Validated family proves momentum edges exist still")
    distinct = _side("Gate failures are the reason the fund gates strategies",
                     "The winner decile excludes this symbol entirely",
                     "Buying ungated exposure is mandate drift",
                     weakest="Accumulation is inference from price alone",
                     concede="The structure has persisted all window")
    assert debate_diversity(_bundle(
        debate=DebateView(bull=bull, bear=echo))).passed is False
    assert debate_diversity(_bundle(
        debate=DebateView(bull=bull, bear=distinct))).passed is True


def test_echoed_points_fail_even_with_disjoint_padding():
    """Finding #7: padding grows the jaccard union but cannot remove a shared
    bigram — a verbatim copy plus color sentences is still an echo."""
    bull = _side("Persistent trend shows institutional accumulation",
                 "Relative strength argues real demand",
                 "Validated family proves momentum edges exist")
    padded_echo = _side(
        "Persistent trend shows institutional accumulation",
        "Relative strength argues real demand",
        "Validated family proves momentum edges exist",
        "Semiconductor capex supercycles and hyperscaler budgets deepen the "
        "demand backdrop this cycle",
        "Index inclusion flows and buyback authorizations add a mechanical "
        "bid under the shares")
    r = debate_diversity(_bundle(debate=DebateView(bull=bull, bear=padded_echo)))
    assert r.score == Decimal("0.0000") and r.passed is False
    assert "shared bigrams" in r.detail


def test_one_sided_debate_fails():
    """Finding #8: a debate where one side said nothing is maximally
    degenerate, not maximally diverse."""
    empty = DebateSideView(strongest_points=(), weakest_opposing_point="",
                           concede="")
    bear = _side("Gate failures are the reason the fund gates strategies",
                 "The winner decile excludes this symbol entirely",
                 "Buying ungated exposure is mandate drift")
    r = debate_diversity(_bundle(debate=DebateView(bull=empty, bear=bear)))
    assert r.score == Decimal("0.0000") and r.passed is False


def test_degenerate_empty_debate_scores_zero():
    empty = DebateSideView(strongest_points=(), weakest_opposing_point="",
                           concede="")
    r = debate_diversity(_bundle(debate=DebateView(bull=empty, bear=empty)))
    assert r.score == Decimal("0.0000") and r.passed is False


def test_interleaved_filler_cannot_launder_a_copied_case():
    """Round-2 finding #3: filler wedged BETWEEN copied words destroys every
    copied adjacent bigram (the padded-echo pin only ever exercised APPENDED
    padding), laundering a majority-verbatim echo past the bigram cap. The
    ordered-containment check reads the copied stem sequence straight through
    the filler: insertion cannot reorder what it copies. SCOPE (round-3
    finding #2): a copier who REORDERS the copied clauses escapes this check
    — the documented v1 limit pinned visibly as echo_chamber_reordered."""
    bull = _side("Persistent trend shows institutional accumulation",
                 "Relative strength argues real demand",
                 "Validated family proves momentum edges exist")
    interleaved = _side(
        "Persistent entrenched trend plainly shows heavy institutional "
        "stealth accumulation everywhere",
        "Relative durable strength frankly argues genuinely real deep demand",
        "Validated house family surely proves tradable momentum type edges "
        "certainly exist")
    r = debate_diversity(_bundle(debate=DebateView(bull=bull, bear=interleaved)))
    assert r.score == Decimal("0.0000") and r.passed is False
    assert "interleaved echo" in r.detail
    # a genuinely opposed case with the same filler vocabulary still passes
    distinct = _side("Gate failures are the reason the fund gates strategies",
                     "The winner decile excludes this symbol entirely",
                     "Buying ungated exposure is plainly mandate drift")
    r = debate_diversity(_bundle(debate=DebateView(bull=bull, bear=distinct)))
    assert r.passed is True


def test_evidence_derived_bigrams_are_shared_vocabulary_not_echo():
    """Round-2 finding #5: both sides are ORDERED to anchor to the same
    corpus and name figures by evidence ID, so hyphenated house terms
    (xsmom-pit, walk-forward folds) produce shared stem bigrams by
    construction — five such mentions alone once exceeded the cap of 4 and
    hard-floored a genuinely opposed debate to 0.0000. Bigrams both of whose
    stems appear in the bundle's own evidence bodies are excluded; the SAME
    debate without that evidence still fails, so the exclusion is not a
    laundering path."""
    evidence = (("quant:families:X:2026-07-10",
                 "Quant record: family xsmom-pit holds a decision-grade PASS "
                 "with walk-forward folds on the point-in-time universe; "
                 "momentum-v1 FAILED its decision gates."),)
    bull = _side("The xsmom-pit family holds a decision-grade PASS with all "
                 "walk-forward folds on the point-in-time universe, so the "
                 "momentum style is validated by the fund's own machinery",
                 "Trend persistence argues institutional accumulation",
                 "Relative strength through a flat tape shows real demand")
    bear = _side("momentum-v1 FAILED its decision gates even though the "
                 "xsmom-pit decision-grade PASS stands, and the walk-forward "
                 "folds never selected this name",
                 "The winner decile excludes the symbol entirely",
                 "Late entries after long runs court classic drawdown risk")
    # five shared house-term bigrams: (xsmom,pit) (deci,grad) (grad,pass)
    # (walk,forward) (forward,fold) — all evidence-derived, so the anchored
    # debate passes...
    with_ev = _bundle(evidence=evidence, debate=DebateView(bull=bull, bear=bear))
    r = debate_diversity(with_ev)
    assert r.passed is True and "echoed bigrams" in r.detail
    # ... and the SAME debate with no evidence on the table gets no exclusion
    # (5 > cap 4): the exclusion cannot become a laundering path
    without_ev = _bundle(evidence=(), debate=DebateView(bull=bull, bear=bear))
    r = debate_diversity(without_ev)
    assert r.score == Decimal("0.0000") and r.passed is False
    assert "shared bigrams" in r.detail


def test_diversity_reads_strongest_points_only():
    """Finding #4: weakest_opposing_point and concede quote/grant the
    opposing case BY DESIGN (schemas/debate.py requires it) — identical
    values there must not drag a genuinely diverse debate toward the echo
    verdict."""
    shared_kwargs = dict(
        weakest="The persistent trend above both moving averages shows "
                "institutional accumulation in the name",
        concede="The persistent trend above both moving averages shows "
                "institutional accumulation in the name")
    bull = _side("Persistent trend shows institutional accumulation",
                 "Relative strength argues real demand",
                 "Validated family proves momentum edges exist",
                 **shared_kwargs)
    bear = _side("Gate failures are the reason the fund gates strategies",
                 "The winner decile excludes this symbol entirely",
                 "Buying ungated exposure is mandate drift",
                 **shared_kwargs)
    r = debate_diversity(_bundle(debate=DebateView(bull=bull, bear=bear)))
    assert r.passed is True
    assert "strongest_points only" in r.detail


# ---------------------------------------------------------------------------
# conviction-rubric conformance
def test_na_conviction_on_directional_memo_fails():
    r = conviction_conformance(_bundle(conviction="N/A"))
    assert r.passed is False and "na_on_directional" in r.detail


def test_insufficient_evidence_conviction_conforms_at_na_or_low():
    """Round-2 finding #6: the CIO template (prompts/cio/committee_memo.md
    line 10) offers only LOW/MEDIUM/HIGH and caps at LOW when
    evidence_available=false — it never elicits N/A, so the old
    na_iff_insufficient XOR failed every prompt-conformant abstention.
    INSUFFICIENT_EVIDENCE conforms with conviction in {N/A, LOW} (N/A stays
    valid for fixtures/legacy rows); MEDIUM/HIGH on an abstention still
    fails."""
    for conv in ("N/A", "LOW"):
        r = conviction_conformance(_bundle(
            recommendation="INSUFFICIENT_EVIDENCE", conviction=conv))
        assert r.passed is True, conv
    for conv in ("MEDIUM", "HIGH"):
        r = conviction_conformance(_bundle(
            recommendation="INSUFFICIENT_EVIDENCE", conviction=conv))
        assert r.passed is False, conv
        assert "ie_conviction_above_low" in r.detail


def test_blank_or_out_of_enum_conviction_fails():
    """Finding #22: '' is exactly what --db mode yields for a NULL conviction
    column; 'EXTREME' is out of the rubric enum. Both are nonconforming on
    any memo — the cage's Literal rejects them, and a labelless row would
    poison the conviction-calibration series."""
    r = conviction_conformance(_bundle(conviction=""))
    assert r.passed is False and "label_out_of_rubric" in r.detail
    r = conviction_conformance(_bundle(conviction="EXTREME"))
    assert r.passed is False and "label_out_of_rubric" in r.detail
    r = conviction_conformance(_bundle(recommendation="BUY", conviction=""))
    assert r.passed is False


def test_medium_or_high_without_evidence_fails():
    r = conviction_conformance(_bundle(evidence=(), evidence_refs=(),
                                       conviction="HIGH"))
    assert r.passed is False and "low_without_evidence" in r.detail


def test_medium_conviction_on_pre_0013_shape_is_not_capped():
    """Finding #16: the run provably attached evidence; only the bodies
    predate migration 0013 — the LOW cap must not fire."""
    r = conviction_conformance(_bundle(evidence=(), conviction="MEDIUM",
                                       run_attached_evidence=True))
    assert r.passed is True


def test_high_conviction_hedged_thesis_fails_at_literal_two_hedges():
    """Boundary literals (finding #20): two hedges fail, one is prose."""
    two = _bundle(conviction="HIGH",
                  thesis="This could work and the tape seems strong.")
    assert hedge_count(two.thesis) == 2
    assert conviction_conformance(two).passed is False
    one = _bundle(conviction="HIGH",
                  thesis="The record is decisive, though timing may vary.")
    assert hedge_count(one.thesis) == 1
    assert conviction_conformance(one).passed is True


def test_hedge_lexicon_covers_common_llm_hedges():
    """Finding #10: probably/likely/potentially/suggests/looks/presumably/
    plausibly/conceivably/somewhat/roughly/tentatively and negated-certainty
    phrases are the CENTER of the LLM hedging distribution."""
    assert hedge_count("This is potentially the moment: the tape looks "
                       "strong, the setup suggests durability, and the "
                       "market is probably underpricing it, though the "
                       "evidence is presumably thin.") == 5
    assert hedge_count("Likely true, plausibly durable, conceivably early, "
                       "somewhat stretched, roughly fair, tentatively "
                       "constructive.") == 6
    assert hedge_count("It is not certain the trend holds and hard to say "
                       "when it breaks.") == 2
    r = conviction_conformance(_bundle(
        conviction="HIGH",
        thesis="The tape looks strong and the market is probably still "
               "underpricing the story."))
    assert r.passed is False and "high_not_hedged" in r.detail


def test_low_conviction_may_hedge_freely():
    r = conviction_conformance(_bundle(
        conviction="LOW",
        thesis="This could possibly work but seems unclear and may fade."))
    assert r.passed is True


def test_hedge_surface_forms_count_without_stem_folding():
    """Round-4 (round-3 findings #5/#6, replacing the round-2 stem fold):
    the lexicon enumerates every counted surface form explicitly. The
    round-2 panel's inflected thesis still counts 8; each enumerated
    inflection counts exactly like its lexeme's other forms; and an
    unhedged HIGH thesis still counts zero."""
    panel = ("The trend seemed durable and the tape appeared institutional; "
             "a possible re-rating is probable, it is plausible and indeed "
             "conceivable that the family selects the name at the next "
             "rebalance, and a tentative reading of the record suggested "
             "continued strength.")
    assert hedge_count(panel) == 8
    r = conviction_conformance(_bundle(conviction="HIGH", thesis=panel))
    assert r.passed is False and "high_not_hedged" in r.detail
    # each enumerated inflection counts exactly like its pinned cousins
    for word in ("seemed", "appeared", "suggested", "suggesting",
                 "possible", "probable", "plausible", "conceivable",
                 "tentative", "arguable",
                 # the -ity/-hood nouns the round-2 stemmer could not reach
                 "possibility", "possibilities", "likelihood",
                 "probability", "plausibility", "uncertainties"):
        assert hedge_count(f"It {word} fine") == 1, word
    # ... and an unhedged HIGH thesis still counts zero
    assert hedge_count("The record is decisive and the committee acts on "
                       "the validated family verdict.") == 0


def test_derivational_hedge_nouns_fail_a_high_thesis():
    """Round-3 finding #5: 'there is a distinct possibility that / in all
    likelihood / the probability of' is the same hedged abstention as
    'possibly / likely / probably', but the -ity/-hood nouns were
    stemmer-unreachable and counted ZERO — a HIGH thesis built of them
    swept conviction_conformance. Surface enumeration counts 3."""
    derivational = ("There is a distinct possibility that the family selects "
                    "the name at the next rebalance, in all likelihood the "
                    "trend extends, and the probability of a re-rating is "
                    "high.")
    assert hedge_count(derivational) == 3
    r = conviction_conformance(_bundle(conviction="HIGH", thesis=derivational))
    assert r.passed is False and "high_not_hedged" in r.detail


def test_confident_noun_potential_is_not_a_hedge():
    """Round-3 finding #6: stem folding mapped potentially -> potential, so
    two plain uses of the noun ('upside potential', 'the potential
    re-rating') failed a hedge-free HIGH thesis. Surface forms pin the
    boundary exactly: 'potentially' always counts, the noun 'potential'
    never does — likewise 'forward-looking', 'apparent' and 'hopeful',
    which the old fold also captured."""
    confident = ("The potential from the validated family PASS is "
                 "substantial, and the potential re-rating at the monthly "
                 "rebalance adds a second leg to the thesis.")
    assert hedge_count(confident) == 0
    r = conviction_conformance(_bundle(conviction="HIGH", thesis=confident))
    assert r.passed is True
    assert hedge_count("It potentially re-rates") == 1
    for word in ("potential", "apparent", "hopeful", "like", "look",
                 "looking", "rough"):
        assert hedge_count(f"It is {word} fine") == 0, word
    assert hedge_count("The forward-looking guidance is decisive.") == 0


# ---------------------------------------------------------------------------
# refs completeness (+ Constitution 4 BUY gates)
def test_dangling_ref_fails_and_is_named():
    r = refs_completeness(_bundle(
        evidence_refs=("dcp:bars:X:2026-07-10", "dcp:ghost:X")))
    assert r.passed is False and "dcp:ghost:X" in r.detail


def test_directional_memo_with_evidence_must_cite():
    r = refs_completeness(_bundle(evidence_refs=()))
    assert r.passed is False and "directional_cites_refs" in r.detail


def test_directional_memo_with_no_evidence_at_all_must_still_cite():
    """Round-2 finding #8: a REJECT persisted with zero evidence rows, zero
    refs and no run-attached evidence swept 1.0000 (the citation check was
    gated on evidence presence) while its BUY twin failed — an unaccountable
    directional verdict passed for pointing the safe direction. The template
    (cio/committee_memo.md lines 20-22) allows empty refs ONLY for
    INSUFFICIENT_EVIDENCE, so every directional memo must cite."""
    r = refs_completeness(_bundle(evidence=(), evidence_refs=()))
    assert r.passed is False and "directional_cites_refs" in r.detail
    # the BUY twin keeps failing its Constitution 4 checks too
    r = refs_completeness(_bundle(recommendation="BUY", evidence=(),
                                  evidence_refs=()))
    assert r.passed is False
    # the pre-0013 not-scoreable shape is unchanged (refs present, bodies
    # predate migration 0013, run provably attached evidence -> n/a)
    r = refs_completeness(_bundle(evidence=(), run_attached_evidence=True))
    assert r.score is None and r.passed is None


def test_buy_without_refs_or_evidence_fails():
    """Finding #19: Constitution 4 re-checked — the cage forbids this shape
    twice over and the judge may not be weaker."""
    r = refs_completeness(_bundle(recommendation="BUY", evidence=(),
                                  evidence_refs=()))
    assert r.passed is False
    assert "buy_cites_refs" in r.detail and "buy_has_dcp_evidence" in r.detail
    ok = refs_completeness(_bundle(recommendation="BUY"))
    assert ok.passed is True


def test_refs_pre_0013_shape_is_not_scoreable():
    """Finding #16: refs cannot be resolved against bodies that were never
    persisted; with run-attached evidence proven this is n/a, and without it
    the same shape stays a dangling-ref FAIL."""
    r = refs_completeness(_bundle(evidence=(), run_attached_evidence=True))
    assert r.score is None and r.passed is None and "0013" in r.detail
    r = refs_completeness(_bundle(evidence=(), run_attached_evidence=False))
    assert r.passed is False


def test_insufficient_evidence_memo_may_cite_nothing():
    r = refs_completeness(_bundle(recommendation="INSUFFICIENT_EVIDENCE",
                                  evidence=(), evidence_refs=()))
    assert r.passed is True


# ---------------------------------------------------------------------------
# bundle verdict semantics
def test_not_applicable_metrics_never_fail_a_bundle():
    s = score_bundle(_bundle())          # no debate: diversity is n/a
    assert s.passed is True
    assert any(r.passed is None for r in s.results)


def test_one_failing_metric_fails_the_bundle():
    s = score_bundle(_bundle(kill_criteria=("Sentiment sours on the story",
                                            "The close falls below the SMA50")))
    assert s.passed is False
