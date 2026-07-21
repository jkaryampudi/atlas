"""F-023: register_trial must bind lineage to a reviewed catalog, so a fresh
arbitrary tag cannot reset the deflated-Sharpe multiple-testing penalty."""
from __future__ import annotations

import pytest

from atlas.dcp.backtest.registry import KNOWN_LINEAGES, register_trial


class _RejectingSession:
    """Fails if register_trial ever reaches the INSERT for a bad lineage."""
    def execute(self, *_a, **_k):
        raise AssertionError("register_trial reached INSERT for an invalid lineage")


def test_unknown_lineage_is_refused_before_insert():
    with pytest.raises(ValueError, match="unknown lineage"):
        register_trial(_RejectingSession(), family="x", spec={}, metrics={},
                       lineage="special-tag")


def test_empty_lineage_still_refused():
    with pytest.raises(ValueError, match="lineage is required"):
        register_trial(_RejectingSession(), family="x", spec={}, metrics={}, lineage="")


def test_catalog_covers_the_real_research_lines():
    # the lines that legitimately exist in the registry
    assert {"momentum", "pead", "trend", "meanrev", "breakout", "quality",
            "low-vol", "fxlab", "momentum+pead"} <= KNOWN_LINEAGES
