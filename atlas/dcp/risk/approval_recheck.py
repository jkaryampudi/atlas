"""Approval-time re-check (Doc 04 §2.2).

The risk check is re-run at human-approval time against a fresh portfolio
snapshot and current prices; a now-FAIL voids the approval action. There is NO
grandfathering: a proposal that passed at proposal time and fails on the fresh
state is dead, and the FRESH check's itemised results are what callers persist
and audit (§2.3: orders reference the check the Execution service verifies).

Pure functions only — L1-L11 semantics live in engine.validate and are reused,
never re-implemented; the trade_proposals state-machine and DB wiring belong
to Phase 5.
"""
from __future__ import annotations

from dataclasses import dataclass

from atlas.dcp.risk.engine import (
    BreakerLevel,
    Limits,
    PortfolioState,
    RiskCheck,
    TradeProposal,
    validate,
)


@dataclass(frozen=True)
class ApprovalRecheck:
    original: RiskCheck   # the proposal-time PASS that gated pending_approval (§2.1)
    fresh: RiskCheck      # authoritative: re-run on the fresh snapshot (§2.2)
    voided: bool          # True -> the approval action is void; FAIL is terminal


def approval_voided(original: RiskCheck, fresh: RiskCheck) -> bool:
    """§2.2: a now-FAIL voids the approval. The verdict is driven ONLY by the
    fresh check — the original cannot rescue it (no grandfathering). The
    original is validated, not consulted: reaching approval without a
    proposal-time PASS violates §2.1, so that misuse fails closed loudly."""
    if not original.passed:
        raise ValueError("original check must be a PASS — §2.1 forbids "
                         "pending_approval without a referenced PASS check")
    return not fresh.passed


def recheck_at_approval(*, proposal: TradeProposal, state_now: PortfolioState,
                        limits: Limits, breaker: BreakerLevel,
                        original_check: RiskCheck) -> ApprovalRecheck:
    """Re-run engine.validate against the FRESH state and breaker (§2.2) and
    return the itemised fresh check alongside the void verdict."""
    fresh = validate(proposal, state_now, limits, breaker)
    return ApprovalRecheck(original=original_check, fresh=fresh,
                           voided=approval_voided(original_check, fresh))
