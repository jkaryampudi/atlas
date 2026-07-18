"""Outcome-labeling pure core (learning loop v1): the specialist alignment
mapping and red-flag validation rule pinned as an exhaustive truth table, the
memo label's direction_vindicated delegated to THE scorecard rule, shadow and
idempotency semantics of plan_labels. DB plumbing is proven in
tests/integration/test_learning_pg.py; this file pins the mapping itself.
"""
from __future__ import annotations

from decimal import Decimal

from atlas.dcp.learning.labeling import (MaturedOutcome, SpecialistView,
                                         flag_validation, plan_labels,
                                         specialist_alignment)

UP = Decimal("0.060000")
DOWN = Decimal("-0.060000")
FLAT = Decimal("0.000000")


def _outcome(memo_id="m1", *, rec="BUY", excess=UP, h=20, shadow=False,
             conviction="HIGH", source=None, symbol="AVGO"):
    return MaturedOutcome(memo_id=memo_id, symbol=symbol, horizon_sessions=h,
                          excess=excess, recommendation=rec,
                          conviction=conviction, source=source, shadow=shadow)


def _seat(memo_id="m1", *, role="quality", stance="concerned",
          confidence="high", flags=2):
    return SpecialistView(memo_id=memo_id, role=role, stance=stance,
                          confidence=confidence, n_red_flags=flags)


# ---------------------------------------------- the mapping, exhaustively

def test_specialist_alignment_truth_table():
    """The module-docstring table, cell by cell: a directional stance is
    aligned only when the realized sign confirms it; the dead heat confirms
    no directional claim; neutral (and unknown) stances grade None."""
    assert specialist_alignment("supportive", UP) is True
    assert specialist_alignment("supportive", DOWN) is False
    assert specialist_alignment("supportive", FLAT) is False
    assert specialist_alignment("concerned", DOWN) is True
    assert specialist_alignment("concerned", UP) is False
    assert specialist_alignment("concerned", FLAT) is False
    assert specialist_alignment("neutral", UP) is None
    assert specialist_alignment("neutral", DOWN) is None
    assert specialist_alignment("neutral", FLAT) is None
    assert specialist_alignment("", UP) is None          # unknown, never a guess


def test_flag_validation_rule():
    """Flags validate when the flagged risk materialized (excess < 0); no
    flags = nothing to validate; the dead heat validates nothing."""
    assert flag_validation(2, DOWN) is True
    assert flag_validation(1, UP) is False
    assert flag_validation(1, FLAT) is False
    assert flag_validation(0, DOWN) is None
    assert flag_validation(0, UP) is None


def test_memo_label_uses_the_scorecard_rule_verbatim():
    """direction_vindicated is scorecard.vindicated — BUY wants excess > 0,
    REJECT wants excess < 0, dead heats vindicate neither, HOLD/WATCHLIST
    and shadow grade None (tracked, never rated)."""
    outs = [
        _outcome("m1", rec="BUY", excess=UP),
        _outcome("m2", rec="BUY", excess=FLAT),
        _outcome("m3", rec="REJECT", excess=DOWN),
        _outcome("m4", rec="REJECT", excess=UP),
        _outcome("m5", rec="WATCHLIST", excess=UP),
        _outcome("m6", rec="HOLD", excess=DOWN),
        _outcome("m7", rec="BUY", excess=UP, shadow=True),
    ]
    memo_labels, _, already = plan_labels(outs, {}, set())
    assert already == 0
    got = {ml.memo_id: ml.direction_vindicated for ml in memo_labels}
    assert got == {"m1": True, "m2": False, "m3": True, "m4": False,
                   "m5": None, "m6": None, "m7": None}
    # the label snapshots the memo's slicing fields verbatim
    m1 = next(ml for ml in memo_labels if ml.memo_id == "m1")
    assert (m1.conviction, m1.source, m1.shadow, m1.excess,
            m1.horizon_sessions) == ("HIGH", None, False, UP, 20)


def test_specialist_labels_graded_per_seat_and_sorted():
    outs = [_outcome("m1", rec="BUY", excess=DOWN)]
    panel = {"m1": [
        _seat("m1", role="quality", stance="concerned", confidence="high",
              flags=2),
        _seat("m1", role="growth", stance="supportive", confidence="medium",
              flags=0),
        _seat("m1", role="macro", stance="neutral", confidence="low", flags=1),
    ]}
    _, spec, _ = plan_labels(outs, panel, set())
    assert [(s.role, s.aligned, s.flag_validated) for s in spec] == [
        ("growth", False, None),        # supportive on a loser, no flags
        ("macro", None, True),          # neutral: no claim; its flag still validated
        ("quality", True, True),        # concerned and right, flags validated
    ]
    assert all(s.horizon_sessions == 20 for s in spec)


def test_shadow_memo_gets_no_specialist_labels():
    """Non-actionable end to end (ADR-0005 pattern 4): the shadow memo's
    outcome is labeled (record complete, vindicated None) but its panel is
    never graded."""
    outs = [_outcome("m1", rec="BUY", excess=UP, shadow=True)]
    panel = {"m1": [_seat("m1")]}
    memo_labels, spec, _ = plan_labels(outs, panel, set())
    assert len(memo_labels) == 1 and memo_labels[0].shadow is True
    assert spec == []


def test_planning_is_idempotent_against_existing_keys():
    """A matured outcome labels once per horizon per seat: existing keys are
    skipped (already counts memo-kind re-encounters), mirrored in the DB by
    the 0030 unique index."""
    outs = [_outcome("m1", h=20), _outcome("m1", h=60)]
    panel = {"m1": [_seat("m1", role="quality")]}
    existing = {("m1", 20, "memo", ""), ("m1", 20, "specialist", "quality")}
    memo_labels, spec, already = plan_labels(outs, panel, existing)
    assert already == 1
    assert [(ml.memo_id, ml.horizon_sessions) for ml in memo_labels] == [("m1", 60)]
    assert [(s.horizon_sessions, s.role) for s in spec] == [(60, "quality")]
    # full re-encounter: clean no-op
    existing |= {("m1", 60, "memo", ""), ("m1", 60, "specialist", "quality")}
    memo_labels, spec, already = plan_labels(outs, panel, existing)
    assert memo_labels == [] and spec == [] and already == 2
