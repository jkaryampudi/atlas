"""Policy-conformance structural checks (risk-wiring bundle, 2026-07-18).

Doc 04 built three protections (§7 stress_marginal_gate, §12
check_factor_overlap, §11 gross_step_gate) that sat with ZERO call sites
outside their own tests — every policy claim must have a LIVE call site, and
an unwiring regression must fail loudly, not silently. These tests are
deliberately grep-based and simple: they scan atlas/ source (never tests/)
for real call expressions, so deleting or bypassing the wiring breaks the
suite even if no behavioral test happens to cross the gate that day.

The DB-backed half — that a really-built proposal PERSISTS every expected
rule row — lives in tests/integration/test_policy_conformance_pg.py.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

ATLAS = Path(__file__).parents[2] / "atlas"


def _call_sites(func_name: str, defining_module: str) -> list[str]:
    """Relative paths of atlas/ modules (excluding the defining module) that
    contain a call expression `func_name(` — imports alone do not count."""
    call = re.compile(rf"(?<!def )\b{re.escape(func_name)}\(")
    hits: list[str] = []
    for path in sorted(ATLAS.rglob("*.py")):
        rel = path.relative_to(ATLAS).as_posix()
        if rel == defining_module:
            continue
        if call.search(path.read_text(encoding="utf-8")):
            hits.append(rel)
    return hits


def test_stress_marginal_gate_has_a_live_call_site():
    sites = _call_sites("stress_marginal_gate", "dcp/risk/stress.py")
    assert sites, ("Doc 04 §7's stress gate has NO live call site — the "
                   "policy claims it, so the proposal path must run it")
    assert "dcp/trading/proposals.py" in sites


def test_check_factor_overlap_has_a_live_call_site():
    sites = _call_sites("check_factor_overlap", "dcp/risk/factor_overlap.py")
    assert sites, ("Doc 04 §12's factor-overlap guard has NO live call site — "
                   "the policy claims it, so the proposal path must run it")
    assert "dcp/trading/proposals.py" in sites


def test_gross_step_gate_has_a_live_call_site():
    sites = _call_sites("gross_step_gate", "dcp/risk/vol_target.py")
    assert sites, ("Doc 04 §11's gross-step gate has NO live call site — the "
                   "policy claims it, so the proposal path must run it")
    assert "dcp/trading/proposals.py" in sites


def test_no_averaging_down_bridge_is_the_only_live_caller_of_build_proposal():
    """Doc 03 prohibited activities — 'no averaging down past the original
    risk budget'. The policy's call site is STRUCTURAL: bridge_memos skips any
    memo whose symbol has an open position, and the bridge is the ONLY live
    caller of build_proposal — so the agent lane cannot reach the add-on merge
    branch at all. If a second caller ever appears, this test forces the
    question of whether that path can average down (bridge.py module
    docstring, no-averaging-down block)."""
    assert _call_sites("build_proposal", "dcp/trading/proposals.py") == [
        "dcp/trading/bridge.py"]


def test_policy_constants_are_pinned():
    """The bundle's constants are policy — changing any of them must be a
    reviewed diff, and this pin is what makes the diff reviewed."""
    from atlas.dcp.risk.stress import STRESS_CRASH_LOSS_LIMIT
    from atlas.dcp.risk.vol_target import MAX_GROSS, STEP_CAP
    from atlas.dcp.trading.bridge import (
        EARNINGS_GUARD_SESSIONS,
        REENTRY_COOLING_SESSIONS,
    )
    assert EARNINGS_GUARD_SESSIONS == 2
    assert REENTRY_COOLING_SESSIONS == 10
    assert STRESS_CRASH_LOSS_LIMIT == Decimal("-0.25")
    # STEP_CAP is fixed §11; the VOL gross ceiling TRACKS L5 (1 - cash floor,
    # Principal 2026-07-18) so it is no longer a module constant. MAX_GROSS is
    # the v1-era default retained only for the unwired Tier-1 scaler.
    assert (MAX_GROSS, STEP_CAP) == (0.80, Decimal("0.10"))
