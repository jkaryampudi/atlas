"""Lessons ledger templating (learning loop v1): the closed one-template
vocabulary pinned with exact golden strings and tags, the retired template
pinned as retired, and the conditions that must NOT produce a lesson.
Deterministic by construction — same labels, same rows, same text.
"""
from __future__ import annotations

from decimal import Decimal

from atlas.dcp.learning.labeling import MemoLabel, SpecialistLabel
from atlas.dcp.learning.lessons import (
    LESSON_TEMPLATES,
    RETIRED_TEMPLATES,
    derive_lessons,
)


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
    assert LESSON_TEMPLATES == ("high_conviction_call_failed",)


def test_retired_template_is_pinned_and_never_live():
    """specialist_flags_validated fired on 32/32 graded BUYs (2026-08-30) —
    retired by name so it cannot quietly return under the same tag."""
    assert RETIRED_TEMPLATES == ("specialist_flags_validated",)
    assert not set(RETIRED_TEMPLATES) & set(LESSON_TEMPLATES)


def test_high_conviction_failure_golden():
    ml = _memo()
    (lesson,) = derive_lessons([ml], [])
    assert lesson.source_type == "memo_outcome"
    assert lesson.source_id == "m1"
    assert lesson.lesson == ("HIGH-conviction BUY on AVGO was not vindicated "
                             "at 20 sessions: excess -8.30% vs SPY — the "
                             "call was wrong.")
    assert lesson.tags == ("high_conviction_call_failed", "h20", "BUY")


def test_lesson_text_never_claims_the_dissent_was_right():
    """dissent_right is NOT vindicated by definition (scorecard rule); a
    lesson that says "the dissent was right" restates the failure as a
    finding. The template must not contain the phrase."""
    for ml in (_memo(), _memo(rec="REJECT", excess=Decimal("0.041000"))):
        (lesson,) = derive_lessons([ml], [])
        assert "dissent" not in lesson.lesson.lower()


def test_high_conviction_failed_reject_also_a_lesson():
    ml = _memo(rec="REJECT", excess=Decimal("0.041000"), h=60)
    (lesson,) = derive_lessons([ml], [])
    assert lesson.lesson == ("HIGH-conviction REJECT on AVGO was not "
                             "vindicated at 60 sessions: excess +4.10% vs "
                             "SPY — the call was wrong.")
    assert lesson.tags == ("high_conviction_call_failed", "h60", "REJECT")


def test_validated_flags_on_failed_buy_derive_no_lesson():
    """The retired template: validated flags on a failed BUY were the
    canonical 'desk was warned' record — and fired on every failed BUY
    because every seat lists flags on every memo. They derive nothing now;
    the flag_validated label itself is still written and measured."""
    ml = _memo()
    lessons = derive_lessons([ml], [_spec(), _spec(role="growth"),
                                    _spec(role="macro")])
    assert [x.tags[0] for x in lessons] == ["high_conviction_call_failed"]
    assert all(t not in RETIRED_TEMPLATES for x in lessons for t in x.tags)


def test_no_lesson_when_nothing_notable():
    """Vindicated calls, non-HIGH failures, and specialist labels of any
    kind (retired template) produce nothing."""
    assert derive_lessons([_memo(vindicated=True, excess=Decimal("0.06"))],
                          []) == ()
    assert derive_lessons([_memo(conviction="MEDIUM")], []) == ()
    assert derive_lessons([_memo(conviction=None)], []) == ()
    rej = _memo(rec="REJECT", vindicated=True)
    assert derive_lessons([rej], [_spec()]) == ()
    ml = _memo()
    assert [x.tags[0] for x in derive_lessons([ml], [_spec(validated=False)])
            ] == ["high_conviction_call_failed"]
    assert derive_lessons([], [_spec()]) == ()


def test_horizons_yield_independent_lessons():
    """The 20s and 60s maturations are separate facts; each failure is its
    own lesson row with its own horizon tag."""
    lessons = derive_lessons([_memo(h=20), _memo(h=60)], [])
    assert [x.tags[1] for x in lessons] == ["h20", "h60"]
