"""Approval-time re-check (Doc 04 §2.2): the check re-runs against the FRESH
snapshot at human-approval time; a now-FAIL voids the approval — no
grandfathering — and the fresh itemised results are what callers persist."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from atlas.dcp.risk.approval_recheck import (
    ApprovalRecheck,
    approval_voided,
    recheck_at_approval,
)
from atlas.dcp.risk.engine import (
    BreakerLevel,
    HoldingRisk,
    PortfolioState,
    TradeProposal,
    limits_from_json,
    validate,
)

SEED = json.loads((Path(__file__).parents[2] / "seeds" / "limit_set_v1.json").read_text())
LIMITS = limits_from_json(1, SEED["limits"])
NAV = Decimal("100000")


def _state(holdings=(), cash=None) -> PortfolioState:
    return PortfolioState(nav_aud=NAV, cash_aud=cash if cash is not None else NAV,
                          holdings=tuple(holdings), new_positions_today=0)


def _proposal() -> TradeProposal:
    return TradeProposal(symbol="AVGO", side="BUY", qty=20, entry_price=Decimal("250"),
                         stop_price=Decimal("245"), fx_to_aud=Decimal("1.5"),
                         instrument_type="stock", sector_gics="Information Technology",
                         india_exposed=False, currency="USD", adv_20d=1_000_000,
                         corr_with_existing={})


def _sector_holding(value: str) -> HoldingRisk:
    return HoldingRisk(symbol="MSFT", value_aud=Decimal(value),
                       sector_gics="Information Technology", india_exposed=False,
                       currency="USD", risk_to_stop_aud=Decimal("500"))


def test_unchanged_state_keeps_the_approval():
    original = validate(_proposal(), _state(), LIMITS)
    assert original.passed
    r = recheck_at_approval(proposal=_proposal(), state_now=_state(), limits=LIMITS,
                            breaker=BreakerLevel.NONE, original_check=original)
    assert isinstance(r, ApprovalRecheck)
    assert not r.voided and r.fresh.passed
    assert len(r.fresh.results) == 12  # itemised DD + L1..L11, ready to persist


def test_pass_then_fail_voids_the_approval_no_grandfathering():
    # PASSED at proposal time on a clean book...
    original = validate(_proposal(), _state(), LIMITS)
    assert original.passed
    # ...but by approval time the IT sector has filled up: L3 now breaches
    # (22,000 held + 7,500 cost = 29.5% > 25%)
    state_now = _state(holdings=[_sector_holding("22000")])
    r = recheck_at_approval(proposal=_proposal(), state_now=state_now, limits=LIMITS,
                            breaker=BreakerLevel.NONE, original_check=original)
    assert r.voided
    assert not r.fresh.passed
    # the fresh check explains itself — its results are what gets audited
    assert [f.rule for f in r.fresh.failures()] == ["L3"]
    assert r.original is original and r.original.passed  # original cannot rescue it


def test_breaker_trip_between_proposal_and_approval_voids():
    original = validate(_proposal(), _state(), LIMITS)
    r = recheck_at_approval(proposal=_proposal(), state_now=_state(), limits=LIMITS,
                            breaker=BreakerLevel.DD2, original_check=original)
    assert r.voided and [f.rule for f in r.fresh.failures()] == ["DD"]


def test_approval_voided_is_driven_only_by_the_fresh_check():
    original = validate(_proposal(), _state(), LIMITS)
    fresh_fail = validate(_proposal(), _state(holdings=[_sector_holding("22000")]),
                          LIMITS)
    assert approval_voided(original, fresh_fail) is True
    assert approval_voided(original, original) is False


def test_approval_voided_rejects_an_original_fail():
    # §2.1: pending_approval requires a referenced PASS — an original FAIL here
    # means the caller bypassed the gate; fail closed loudly.
    failed = validate(_proposal(), _state(holdings=[_sector_holding("22000")]),
                      LIMITS)
    fresh = validate(_proposal(), _state(), LIMITS)
    with pytest.raises(ValueError, match="PASS"):
        approval_voided(failed, fresh)
