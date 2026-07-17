"""SpecialistAssessment schema gates (ADR-0011 step 2): closed stance and
confidence vocabularies, key-point and red-flag cardinality bounds, and the
Constitution 3.1 execution-shaped-number ban — validation is a security
control; an output failing these models is a failed run, full stop."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.agents.schemas.specialist import SpecialistAssessment

GOOD = {
    "stance": "supportive",
    "key_points": ["Margins in the fundamentals ref are wide and stable",
                   "Net cash position per the fundamentals ref removes balance-sheet risk"],
    "red_flags": ["Operating margin is reported while no cash-flow fact appears "
                  "in the snapshot"],
    "confidence": "medium",
}


def test_valid_assessment_passes():
    a = SpecialistAssessment.model_validate(GOOD)
    assert a.stance == "supportive" and a.confidence == "medium"
    assert len(a.key_points) == 2 and len(a.red_flags) == 1


@pytest.mark.parametrize("stance", ["bullish", "SUPPORTIVE", "hold", ""])
def test_stance_vocabulary_is_closed(stance):
    with pytest.raises(ValidationError):
        SpecialistAssessment.model_validate({**GOOD, "stance": stance})


@pytest.mark.parametrize("confidence", ["HIGH", "certain", "", "very high"])
def test_confidence_vocabulary_is_closed(confidence):
    with pytest.raises(ValidationError):
        SpecialistAssessment.model_validate({**GOOD, "confidence": confidence})


@pytest.mark.parametrize("n", [0, 1, 5])
def test_key_points_cardinality_bounds(n):
    points = [f"Distinct grounded observation number {'x' * (i + 1)}" for i in range(n)]
    with pytest.raises(ValidationError):
        SpecialistAssessment.model_validate({**GOOD, "key_points": points})


def test_two_to_four_key_points_pass():
    for n in (2, 3, 4):
        points = [f"Observation {'x' * (i + 1)} anchored in the cited evidence"
                  for i in range(n)]
        SpecialistAssessment.model_validate({**GOOD, "key_points": points})


def test_red_flags_capped_at_three_and_may_be_empty():
    SpecialistAssessment.model_validate({**GOOD, "red_flags": []})
    with pytest.raises(ValidationError):
        SpecialistAssessment.model_validate(
            {**GOOD, "red_flags": ["one real flag", "two real flags",
                                   "three real flags", "four is padding"]})


def test_blank_items_are_rejected():
    with pytest.raises(ValidationError):
        SpecialistAssessment.model_validate({**GOOD, "red_flags": ["   "]})
    with pytest.raises(ValidationError):
        SpecialistAssessment.model_validate(
            {**GOOD, "key_points": ["A real grounded point", "  "]})


@pytest.mark.parametrize("smuggle", [
    "Fair value implies $180 by year end",
    "ROE of 42.5% justifies the stance",
    "Entry near 172.40 looks attractive",
    "Set the stop at 150 and forget it",
])
def test_execution_shaped_numbers_fail_in_key_points_and_red_flags(smuggle):
    with pytest.raises(ValidationError):
        SpecialistAssessment.model_validate(
            {**GOOD, "key_points": [GOOD["key_points"][0], smuggle]})
    with pytest.raises(ValidationError):
        SpecialistAssessment.model_validate({**GOOD, "red_flags": [smuggle]})
