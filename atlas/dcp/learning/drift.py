"""Two-sided CUSUM drift detector for live-vs-backtest strategy returns (Doc 04 §13).

Feed daily (live_return - expected_return) residuals. A breach on either side signals
regime-inconsistent performance and triggers Tier 1 auto-demotion to paper.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CusumDetector:
    k: float          # slack per observation (in residual units, e.g. 0.5 * daily sigma)
    h: float          # decision threshold (e.g. 5 * daily sigma)
    pos: float = 0.0
    neg: float = 0.0
    breached: bool = field(default=False)

    def update(self, residual: float) -> bool:
        """Returns True on breach (latched until reset)."""
        if self.breached:
            return True
        self.pos = max(0.0, self.pos + residual - self.k)
        self.neg = max(0.0, self.neg - residual - self.k)
        if self.pos > self.h or self.neg > self.h:
            self.breached = True
        return self.breached

    def reset(self) -> None:
        self.pos = self.neg = 0.0
        self.breached = False
