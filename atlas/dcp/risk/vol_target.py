"""Portfolio volatility targeting (Doc 04 §11, Tier 1 self-correction).

Pure functions only:
- `target_gross_exposure` proposes the next gross exposure. Hard properties,
  tested: never exceeds MAX_GROSS (0.80 — the 20% cash floor L5), never
  negative, bounded daily step (MAX_STEP of NAV), breaker states dominate
  (DD1-DD3 never scale up).
- `gross_step_gate` is the same §11 bounds expressed as a proposal-time
  RuleResult('VOL') — the live call site is build_proposal (risk-wiring
  bundle 2026-07-18): post-trade gross must stay <= the gross ceiling, and
  the day's cumulative gross increase must stay <= MAX_STEP.

  Gross ceiling TRACKS L5 (Principal decision 2026-07-18): the caller passes
  `gross_cap = 1 - active L5 cash floor`, so under limit_set v2 (L5=0.10)
  the ceiling is 0.90, matching ADR-0014's signed allocation — a later
  signed allocation change (e.g. re-funding PEAD) can no longer silently
  collide with a stale hardcoded ceiling. MAX_GROSS below is the v1-era
  default (0.80 = the old 20% cash floor) kept only for the UNWIRED Tier-1
  scaler `target_gross_exposure`; the wired gate never reads it.

  DD interplay (Principal decision 2026-07-18: match Doc 04 §5): DD1 means
  "new-position risk HALVED (L6 -> 0.5%)" — entries are still allowed, and
  the L6 halving in engine.validate is the DD1 remedy, so the VOL gate does
  NOT block a gross increase under DD1. Only DD2 ("no new positions") and
  DD3 ("exit-only") forbid a gross increase here — defense-in-depth beside
  engine.validate's own DD gate. Exits never reach this gate: the exit path
  does not run buy-side validation (Doc 04 §5, exits release risk).
"""
from __future__ import annotations

from decimal import Decimal

from atlas.dcp.risk.engine import BreakerLevel, RuleResult

MAX_GROSS = 0.80        # v1-era default for the UNWIRED scaler only (see docstring)
MAX_STEP = 0.10

# Decimal view of the SAME §11 step constant for exact ledger math in the gate —
# derived, never restated, so the numbers cannot drift apart.
STEP_CAP = Decimal(str(MAX_STEP))

# DD breaker levels that forbid a gross increase in the wired gate. DD1 is
# ABSENT by Principal decision (2026-07-18): Doc 04 §5 lets DD1 buys through
# at halved L6 risk; the L6 halving is the DD1 remedy, not a gross freeze.
_GROSS_FREEZE_BREAKERS = (BreakerLevel.DD2, BreakerLevel.DD3)


def gross_step_gate(*, gross_after: Decimal, step_after: Decimal,
                    breaker: BreakerLevel, gross_cap: Decimal) -> RuleResult:
    """§11 Tier 1 as a proposal gate: one itemised RuleResult('VOL').

    Inputs are NAV fractions computed by the caller from the worst-case
    pro-forma book: `gross_after` is post-trade gross exposure (holdings plus
    pending buy orders plus this proposal), `step_after` is the day's
    cumulative committed gross increase including this proposal. `gross_cap`
    is `1 - active L5 cash floor` (the ceiling TRACKS L5 — Principal
    2026-07-18). Like engine.validate, every bound is always evaluated and a
    FAIL itemises every breach — a FAIL must explain itself completely."""
    breaches: list[str] = []
    if breaker in _GROSS_FREEZE_BREAKERS:
        breaches.append(f"breaker {breaker.value} forbids gross increases "
                        "(§11: DD2/DD3 dominate; DD1 entries allowed at halved "
                        "L6 risk per §5; exits always allowed)")
    if gross_after > gross_cap:
        breaches.append(f"post-trade gross {gross_after:.4f} > max {gross_cap:.2f}")
    if step_after > STEP_CAP:
        breaches.append(f"day gross increase {step_after:.4f} "
                        f"> max step {STEP_CAP:.2f}")
    detail = (f"post-trade gross {gross_after:.4f} vs max {gross_cap:.2f} "
              f"(= 1 - L5), day gross increase {step_after:.4f} vs max step "
              f"{STEP_CAP:.2f}, breaker {breaker.value}")
    if breaches:
        detail += "; BREACH: " + "; ".join(breaches)
    return RuleResult("VOL", not breaches, detail,
                      value=gross_after, limit=gross_cap)


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
