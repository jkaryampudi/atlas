"""Calibration computation pure core (learning loop v1): the three snapshot
row families driven through the EXISTING calibration.py math with hand-computed
goldens (nothing reimplemented — the test derives the expected numbers from
ADR-0003's constants by hand and pins them), specialist reliability
arithmetic, and per-source trust vs the dartboard.

Hand math used below (calibration.py: BASELINE_BRIER=0.25, GAIN=4,
SHRINKAGE_K=30, clip [0.5, 1.5]; CONVICTION_PROB HIGH=0.75, MEDIUM=0.65):

- HIGH, 2 hits 1 miss:  brier = (2*(0.25)^2 + (0.75)^2)/3 = 0.6875/3
                              = 0.02291666...*10 -> 0.2291666...
  edge = 0.25 - 0.2291666... = 0.0208333...; raw = 1.0833333...
  weight = 1 + 0.0833333...*(3/33) = 1.0075757575...
- MEDIUM, 1 hit 1 miss: brier = ((0.35)^2 + (0.65)^2)/2 = 0.545/2 = 0.2725
  edge = -0.0225; raw = 0.91; weight = 1 - 0.09*(2/32) = 0.994375
- specialist quality, (high, aligned) + (medium, unaligned):
  brier = ((0.25)^2 + (0.65)^2)/2 = 0.485/2 = 0.2425
  edge = 0.0075; raw = 1.03; weight = 1 + 0.03*(2/32) = 1.001875
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from atlas.dcp.learning.labeling import MemoLabel, SpecialistLabel
from atlas.dcp.learning.recalibrate import (plan_rows, source_trust,
                                            specialist_reliability)

UP = Decimal("0.060000")
DOWN = Decimal("-0.060000")
FLAT = Decimal("0.000000")


def _memo(memo_id, *, rec="BUY", conviction="HIGH", vindicated=True,
          excess=UP, h=20, source=None, shadow=False):
    return MemoLabel(memo_id=memo_id, symbol="T", horizon_sessions=h,
                     recommendation=rec, conviction=conviction, source=source,
                     shadow=shadow, excess=excess,
                     direction_vindicated=vindicated)


def _spec(memo_id, *, role="quality", stance="concerned", confidence="high",
          flags=0, aligned=True, validated=None, h=20):
    return SpecialistLabel(memo_id=memo_id, horizon_sessions=h, role=role,
                           stance=stance, confidence=confidence,
                           n_red_flags=flags, aligned=aligned,
                           flag_validated=validated)


# ------------------------------------------------ family 1: conviction ladder

def test_conviction_rows_hand_computed_through_existing_math():
    memo_labels = [
        _memo("m1", conviction="HIGH", vindicated=True),
        _memo("m2", conviction="HIGH", vindicated=True),
        _memo("m3", conviction="HIGH", vindicated=False, excess=DOWN),
        _memo("m4", conviction="MEDIUM", vindicated=True),
        _memo("m5", conviction="MEDIUM", vindicated=False, excess=DOWN),
    ]
    rows = {r.agent_role: r for r in plan_rows(memo_labels, [])}
    high = rows["conviction:HIGH"]
    assert high.n_forecasts == 3
    assert high.brier == pytest.approx(0.6875 / 3)
    assert high.weight == pytest.approx(1 + (4 * (0.25 - 0.6875 / 3)) * (3 / 33))
    assert high.weight == pytest.approx(1.00757575757, abs=1e-9)
    med = rows["conviction:MEDIUM"]
    assert med.n_forecasts == 2
    assert med.brier == pytest.approx(0.2725)
    assert med.weight == pytest.approx(0.994375)
    # canonical family order: HIGH, MEDIUM, then the source family
    assert [r.agent_role for r in plan_rows(memo_labels, [])] == [
        "conviction:HIGH", "conviction:MEDIUM", "source:desk nightly"]


def test_ungraded_shadow_and_unscoreable_convictions_are_excluded():
    memo_labels = [
        _memo("m1", rec="HOLD", vindicated=None),          # no direction
        _memo("m2", shadow=True, vindicated=None),         # non-actionable
        _memo("m3", conviction="N/A", vindicated=True),    # unscoreable claim
        _memo("m4", conviction=None, vindicated=True),
    ]
    assert plan_rows(memo_labels, []) == []


# --------------------------------------------- family 2: specialist seats

def test_specialist_rows_and_reliability_arithmetic():
    spec_labels = [
        _spec("m1", role="quality", confidence="high", aligned=True,
              flags=2, validated=True),
        _spec("m2", role="quality", confidence="medium", aligned=False,
              flags=1, validated=False),
        _spec("m3", role="quality", stance="neutral", confidence="low",
              aligned=None, flags=1, validated=True),   # no claim, flag graded
        _spec("m1", role="growth", stance="supportive", confidence="low",
              aligned=True),
    ]
    rows = {r.agent_role: r for r in plan_rows([], spec_labels)}
    q = rows["specialist:quality"]
    assert q.n_forecasts == 2                       # neutral seat carries no claim
    assert q.brier == pytest.approx(0.2425)
    assert q.weight == pytest.approx(1.001875)
    g = rows["specialist:growth"]
    assert g.n_forecasts == 1

    rel = {r.role: r for r in specialist_reliability(spec_labels)}
    assert (rel["quality"].n_graded, rel["quality"].n_aligned) == (2, 1)
    assert rel["quality"].alignment_rate == pytest.approx(0.5)
    assert (rel["quality"].n_flagged, rel["quality"].n_flags_validated) == (3, 2)
    assert rel["quality"].flag_validation_rate == pytest.approx(2 / 3)
    assert rel["growth"].n_flagged == 0
    assert rel["growth"].flag_validation_rate is None
    # canonical role order
    assert [r.role for r in specialist_reliability(spec_labels)] == [
        "quality", "growth"]


# --------------------------------------------- family 3: source trust vs dart

def _trust_fixture():
    """h20 universe of five tracked outcomes: +6 (BUY desk), -6 (BUY
    investing.com), 0 (HOLD desk), -2 (REJECT investing.com), +1 (shadow).
    Dart: BUY baseline 2/5, REJECT baseline 2/5 (the dead heat counts for
    neither — the scorecard's own rule)."""
    return [
        _memo("m1", rec="BUY", vindicated=True, excess=UP),
        _memo("m2", rec="BUY", vindicated=False, excess=DOWN,
              source="investing.com"),
        _memo("m3", rec="HOLD", vindicated=None, excess=FLAT),
        _memo("m4", rec="REJECT", vindicated=True,
              excess=Decimal("-0.020000"), source="investing.com"),
        _memo("m5", rec="BUY", vindicated=None, excess=Decimal("0.010000"),
              shadow=True),
    ]


def test_source_trust_vindication_vs_dartboard():
    trust = source_trust(_trust_fixture())
    assert sorted(trust) == ["desk nightly", "investing.com"]
    desk = trust["desk nightly"][20]
    assert (desk.n_graded, desk.n_vindicated) == (1, 1)
    assert desk.rate == pytest.approx(1.0)
    assert desk.baseline == pytest.approx(0.4)      # BUY dart: 2/5
    assert desk.edge == pytest.approx(0.6)
    ext = trust["investing.com"][20]
    assert (ext.n_graded, ext.n_vindicated) == (2, 1)
    assert ext.rate == pytest.approx(0.5)
    # mixed BUY+REJECT book: mean of each call's own direction-dart (both 0.4)
    assert ext.baseline == pytest.approx(0.4)
    assert ext.edge == pytest.approx(0.1)
    # nothing graded at 60s yet -> no 60s entries at all
    assert 60 not in trust["desk nightly"]
    assert 60 not in trust["investing.com"]


def test_source_rows_bucket_by_source_with_desk_label_for_null():
    rows = {r.agent_role: r for r in plan_rows(_trust_fixture(), [])}
    assert rows["source:desk nightly"].n_forecasts == 1
    assert rows["source:investing.com"].n_forecasts == 2
