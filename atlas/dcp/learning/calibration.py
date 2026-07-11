"""Agent conviction calibration (ADR-0003, Tier 1).

Conviction labels map to implied probabilities; realised outcomes score them (Brier).
Weights update via shrinkage so small samples move slowly (Constitution 10.3) and are
clipped to [0.5, 1.5] so no agent can be silenced or made dominant by the loop.
"""
from __future__ import annotations

from dataclasses import dataclass

CONVICTION_PROB = {"LOW": 0.55, "MEDIUM": 0.65, "HIGH": 0.75}
BASELINE_BRIER = 0.25  # score of an uninformative 0.5 forecast
WEIGHT_MIN, WEIGHT_MAX = 0.5, 1.5
SHRINKAGE_K = 30       # pseudo-observations; ~30 outcomes to earn half the raw shift
GAIN = 4.0             # raw sensitivity of weight to Brier edge


@dataclass(frozen=True)
class Forecast:
    conviction: str   # LOW | MEDIUM | HIGH
    outcome: bool     # thesis played out (per outcome_labels)


def brier_score(forecasts: list[Forecast]) -> float:
    if not forecasts:
        raise ValueError("no forecasts to score")
    total = 0.0
    for f in forecasts:
        p = CONVICTION_PROB[f.conviction]
        total += (p - (1.0 if f.outcome else 0.0)) ** 2
    return total / len(forecasts)


def conviction_weight(forecasts: list[Forecast], *, prev_weight: float = 1.0) -> float:
    """New committee weight for an agent. Better-than-baseline calibration earns weight,
    worse loses it — slowly, bounded, and anchored at 1.0 for tiny samples."""
    n = len(forecasts)
    if n == 0:
        return prev_weight
    edge = BASELINE_BRIER - brier_score(forecasts)      # + = better than uninformative
    raw = 1.0 + GAIN * edge
    shrunk = 1.0 + (raw - 1.0) * (n / (n + SHRINKAGE_K))
    return max(WEIGHT_MIN, min(WEIGHT_MAX, shrunk))
