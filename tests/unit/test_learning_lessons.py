"""Lessons ledger templating (learning loop v1): the closed two-template
vocabulary pinned with exact golden strings and tags, and the conditions that
must NOT produce a lesson. Deterministic by construction — same labels, same
rows, same text.
"""
from __future__ import annotations

from decimal import Decimal

from atlas.dcp.learning.labeling import MemoLabel, SpecialistLabel
from atlas.dcp.learning.lessons import LESSON_TEMPLATES, derive_lessons


def _memo(memo_id="m1", *, rec="BUY", conviction="HIGH", vindicated=False,
          excess=Decimal("-0.083000"), h=20, symbol="AVGO", shadow=False):
    return MemoLabel(memo_id=memo_id, symbol=symbol, horizon_sessions=h,
                     recommendation=rec, conviction=conviction, source=None,
                     shadow=shadow, excess=excess,
                     direction_vindicated=vindicated)


def _spec(memo_id="m1", *, role="quality", flags=2, validated=True, h=20):
    return SpecialistLabel(memo_id=memo_id, horizon_sessions=h, role=role,
                           stance="concerned", confidence="high",
                           n_red_flags=flags, aligned=True,
                           flag_validated=validated)


def test_vocabulary_is_closed():
    assert LESSON_TEMPLATES == ("high_conviction_call_failed",
                                "specialist_flags_validated")


def test_high_conviction_failure_golden():
    ml = _memo()
    (lesson,) = derive_lessons([ml], [])
    assert lesson.source_type == "memo_outcome"
    assert lesson.source_id == "m1"
    assert lesson.lesson == ("HIGH-conviction BUY on AVGO was not vindicated "
                             "at 20 sessions: excess -8.30% vs SPY — the "
                             "dissent was right.")
    assert lesson.tags == ("high_conviction_call_failed", "h20", "BUY")


def test_high_conviction_failed_reject_also_a_lesson():
    ml = _memo(rec="REJECT", excess=Decimal("0.041000"), h=60)
    (lesson,) = derive_lessons([ml], [])
    assert lesson.lesson == ("HIGH-conviction REJECT on AVGO was not "
                             "vindicated at 60 sessions: excess +4.10% vs "
                             "SPY — the dissent was right.")
    assert lesson.tags == ("high_conviction_call_failed", "h60", "REJECT")


def test_flags_validated_on_failed_buy_golden():
    ml = _memo()
    sl = _spec()
    lessons = derive_lessons([ml], [sl])
    assert [x.tags[0] for x in lessons] == ["high_conviction_call_failed",
                                           "specialist_flags_validated"]
    flagged = lessons[1]
    assert flagged.source_id == "m1"
    assert flagged.lesson == ("quality specialist flagged 2 risk(s) on AVGO "
                              "before the BUY; the call failed at 20 sessions "
                              "(excess -8.30% vs SPY) — flags validated.")
    assert flagged.tags == ("specialist_flags_validated", "h20", "BUY",
                            "quality")


def test_no_lesson_when_nothing_notable():
    """Vindicated calls, non-HIGH failures, validated flags on a REJECT (a
    vindicated dodge is reinforcement, not a lesson), unvalidated flags, and
    specialist labels whose memo label is not in the SAME new batch (label
    idempotency IS lesson idempotency) all produce nothing."""
    assert derive_lessons([_memo(vindicated=True, excess=Decimal("0.06"))],
                          []) == ()
    assert derive_lessons([_memo(conviction="MEDIUM")], []) == ()
    assert derive_lessons([_memo(conviction=None)], []) == ()
    # REJECT with excess < 0 is vindicated — flags supported a correct dodge
    rej = _memo(rec="REJECT", vindicated=True)
    assert derive_lessons([rej], [_spec()]) == ()
    # flags present but not validated
    ml = _memo()
    assert [x.tags[0] for x in derive_lessons([ml], [_spec(validated=False)])
            ] == ["high_conviction_call_failed"]
    # specialist label alone (its memo label already recorded on a prior
    # night): no memo context in the new batch, no lesson
    assert derive_lessons([], [_spec()]) == ()


def test_horizons_yield_independent_lessons():
    """The 20s and 60s maturations are separate facts; each failure is its
    own lesson row with its own horizon tag."""
    lessons = derive_lessons([_memo(h=20), _memo(h=60)], [])
    assert [x.tags[1] for x in lessons] == ["h20", "h60"]
