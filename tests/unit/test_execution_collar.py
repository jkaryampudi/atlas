"""M35 execution price-collar unit tests: pin the collar VALUE and the symmetric
|drift| arithmetic, so a regression of the tunable constant or of the abs() to a
signed comparison is caught here (the integration tests bracket behaviour, but a
plausible value/sign regression can slip through a wide bracket — adversarial
review, this cycle)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from atlas.dcp.execution.paper import Fill
from atlas.dcp.trading.proposals import (
    EXECUTION_PRICE_COLLAR_BPS,
    _fill_price_drift_bps,
)


def _fill(fill_price: str, decision_price: str) -> Fill:
    return Fill(
        fill_date=date(2026, 7, 14), fill_qty=10, fill_price=Decimal(fill_price),
        fees=Decimal(0), fx_to_aud=Decimal(1), decision_price=Decimal(decision_price),
        shortfall_bps=Decimal(0),
        executed_at=datetime(2026, 7, 14, 13, 30, tzinfo=UTC))


def test_collar_default_is_ten_percent():
    """Pin the value: a silent widening (e.g. to 2000 bps) would let a 15-20%
    buy gap deploy capital at an untrusted price with the behaviour tests still
    green. This is the direct guard against that."""
    assert EXECUTION_PRICE_COLLAR_BPS == Decimal("1000")


def test_drift_is_symmetric_absolute():
    """|drift| — an equal up-gap and down-gap trip the collar identically, so a
    downward vendor glitch is caught as readily as an adverse up-gap. A regression
    of abs() to a signed comparison would break this (down-drift would go negative
    and never exceed the collar)."""
    up = _fill_price_drift_bps(_fill("110", "100"))       # +10%
    down = _fill_price_drift_bps(_fill("90", "100"))      # -10%
    assert up == down == Decimal("1000")
    # a downward decimal-error glitch (100 -> 0.01) is ~9999 bps, far past the collar
    assert _fill_price_drift_bps(_fill("0.01", "100")) > EXECUTION_PRICE_COLLAR_BPS


def test_boundary_is_strict_greater_than():
    """Exactly at the collar fills (the gate is drift > collar); a hair over
    exceeds. Pins the boundary direction so a >= regression can't tighten it."""
    assert _fill_price_drift_bps(_fill("110", "100")) == EXECUTION_PRICE_COLLAR_BPS
    assert _fill_price_drift_bps(_fill("110.01", "100")) > EXECUTION_PRICE_COLLAR_BPS
    assert _fill_price_drift_bps(_fill("109.99", "100")) < EXECUTION_PRICE_COLLAR_BPS
