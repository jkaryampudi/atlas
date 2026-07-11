"""Portfolio volatility targeting (Doc 04 §11, Tier 1 self-correction).

Pure function: proposes the next gross exposure. Hard properties, tested:
- never exceeds MAX_GROSS (0.80 — the 20% cash floor L5)
- never negative
- bounded daily step (MAX_STEP of NAV)
- breaker states dominate: DD2/DD3 force no-increase / exit-only respectively
"""
from __future__ import annotations

MAX_GROSS = 0.80
MAX_STEP = 0.10


def target_gross_exposure(*, current_gross: float, realised_vol: float,
                          target_vol: float, breaker_level: str = "none") -> float:
    if not (0.0 <= current_gross <= 1.0):
        raise ValueError("current_gross out of range")
    if realised_vol <= 0 or target_vol <= 0:
        raise ValueError("vols must be positive")

    ideal = min(MAX_GROSS, current_gross * (target_vol / realised_vol)
                if current_gross > 0 else min(MAX_GROSS, target_vol / realised_vol))
    step = max(-MAX_STEP, min(MAX_STEP, ideal - current_gross))
    proposed = current_gross + step

    if breaker_level == "DD3":
        proposed = min(proposed, current_gross)   # exit-only: never scale up
        proposed = min(proposed, current_gross)   # (reductions come from exits, not scaler)
    elif breaker_level in ("DD1", "DD2"):
        proposed = min(proposed, current_gross)   # risk-reduction states: no increases

    return max(0.0, min(MAX_GROSS, proposed))
