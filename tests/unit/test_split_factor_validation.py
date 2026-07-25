"""F-014: only genuine split / reverse-split ratios may drive price adjustment.
Non-split vendor factors (1:1 no-ops, cash-dividend-shaped factors, malformed or
absurd values) must be quarantined, never recorded as splits."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from atlas.dcp.market_data.ingest import is_valid_split_ratio, record_split
from atlas.dcp.market_data.models import Split


@pytest.mark.parametrize("ratio", ["2", "3", "0.5", "0.1", "1.5", "20", "0.05"])
def test_genuine_split_ratios_are_valid(ratio: str):
    assert is_valid_split_ratio(Decimal(ratio))


@pytest.mark.parametrize("ratio", ["1", "0", "-2", "1000000", "0.0000001", "NaN", "Infinity"])
def test_non_split_or_degenerate_ratios_are_rejected(ratio: str):
    assert not is_valid_split_ratio(Decimal(ratio))


class _RejectingSession:
    def execute(self, *_a, **_k):
        raise AssertionError("record_split wrote a row for an invalid split ratio")


@pytest.mark.parametrize("ratio", ["1", "0", "-1", "NaN"])
def test_record_split_quarantines_invalid_ratios(ratio: str):
    ok = record_split(_RejectingSession(), "iid",
                      Split(symbol="X", action_date=date(2024, 1, 2), ratio=Decimal(ratio)),
                      "Test")
    assert ok is False        # skipped, no INSERT attempted


class _CapturingSession:
    def __init__(self):
        self.wrote = False

    def execute(self, *_a, **_k):
        self.wrote = True


def test_record_split_writes_a_genuine_split():
    s = _CapturingSession()
    ok = record_split(s, "iid",
                      Split(symbol="X", action_date=date(2024, 1, 2), ratio=Decimal("2")),
                      "Test")
    assert ok is True and s.wrote
