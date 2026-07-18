"""Learning-loop nightly step (v1: label + measure + surface): the one entry
point the T9 cycle calls, after the scorecard has graded tonight's matured
outcomes.

Order is causal: labeling first (matured research.memo_outcomes rows ->
learning.outcome_labels + lessons), then — only when NEW labels landed — a
calibration snapshot (recalibrate.py). A night with nothing newly matured is
a clean no-op: no labels, no snapshot, no audit noise. Nothing here changes
any behavior anywhere (Article 10 discipline, recalibrate.py docstring):
weights are computed and stored, never applied — activation is a Principal
decision.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.learning.labeling import LabelingReport, label_matured_outcomes
from atlas.dcp.learning.recalibrate import CalibrationReport, snapshot_calibration


@dataclass(frozen=True)
class LearningReport:
    labeling: LabelingReport
    calibration: CalibrationReport | None   # None = nothing new, no snapshot

    def summary(self) -> str:
        if self.calibration is None:
            return self.labeling.summary()
        return f"{self.labeling.summary()} · {self.calibration.summary()}"


def run_learning(session: Session, clock: Clock) -> LearningReport:
    lab = label_matured_outcomes(session, clock)
    cal = None
    if lab.memo_labels or lab.specialist_labels:
        cal = snapshot_calibration(session, clock)
    return LearningReport(labeling=lab, calibration=cal)
