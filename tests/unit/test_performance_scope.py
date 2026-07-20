"""P0.1 (ADR-0018) canonical classification + performance-scope vocabulary:
the single fail-closed source of truth for authoritative vs research-shadow vs
non-authoritative, and the governance guard that refuses any non-authoritative
scope. Pure functions — no DB."""
from __future__ import annotations

import pytest

from atlas.dcp import strategy_lifecycle as sl


@pytest.mark.parametrize("state, expected", [
    ("paper", sl.AUTHORITATIVE),
    ("live", sl.AUTHORITATIVE),
    ("research_shadow", sl.RESEARCH_SHADOW),
    ("suspended", sl.NON_AUTHORITATIVE),
    ("validated", sl.NON_AUTHORITATIVE),
    ("backtested", sl.NON_AUTHORITATIVE),
    ("draft", sl.NON_AUTHORITATIVE),
    ("retired", sl.NON_AUTHORITATIVE),
    (None, sl.NON_AUTHORITATIVE),
    ("totally-unknown-state", sl.NON_AUTHORITATIVE),   # fail closed
])
def test_classify_is_fail_closed(state, expected):
    assert sl.classify(state) == expected
    # an unknown/None state is NEVER authoritative
    assert (sl.classify(state) == sl.AUTHORITATIVE) == (state in ("paper", "live"))


def test_normalize_scope_defaults_to_authoritative_and_refuses_unknown():
    assert sl.normalize_scope(None) == sl.AUTHORITATIVE_PORTFOLIO
    assert sl.normalize_scope(sl.RESEARCH_SHADOW_SCOPE) == "research_shadow"
    with pytest.raises(ValueError, match="unknown performance_scope"):
        sl.normalize_scope("include_all")


def test_scope_authoritativeness_and_caveats():
    assert sl.scope_is_authoritative(sl.AUTHORITATIVE_PORTFOLIO) is True
    assert sl.scope_is_authoritative(sl.RESEARCH_SHADOW_SCOPE) is False
    assert sl.scope_is_authoritative(sl.ALL_SIMULATED) is False
    assert sl.scope_caveat(sl.AUTHORITATIVE_PORTFOLIO) == ""
    assert sl.scope_caveat(sl.RESEARCH_SHADOW_SCOPE) == "RESEARCH SHADOW — NOT VALIDATED"
    assert sl.scope_caveat(sl.ALL_SIMULATED) == "COMBINED SIMULATION — NON-AUTHORITATIVE"


def test_require_authoritative_scope_is_the_governance_gate():
    # the only scope governance may consume
    sl.require_authoritative_scope(sl.AUTHORITATIVE_PORTFOLIO)   # no raise
    # known non-authoritative scopes: refused with the governance message
    for bad in (sl.RESEARCH_SHADOW_SCOPE, sl.ALL_SIMULATED):
        with pytest.raises(ValueError, match="governance calculations require"):
            sl.require_authoritative_scope(bad)
    # an unknown scope also fails closed (refused, never widened to authoritative)
    with pytest.raises(ValueError):
        sl.require_authoritative_scope("include_all")
