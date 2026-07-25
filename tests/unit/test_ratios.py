"""F-010: the shared currency/unit normalisation layer (research/ratios.py) and
the conformance that the ratio-forming research modules route through it rather
than re-implementing the currency-compatibility rule inline."""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas.dcp.research.ratios import (
    cross_currency_ratio,
    currencies_confirmed_same,
    currencies_incompatible,
    norm_currency,
)


@pytest.mark.parametrize("value,expected", [
    ("usd", "USD"), (" usd ", "USD"), ("USD", "USD"),
    (None, None), ("", None), ("US", None), ("dollars", None), (123, None),
])
def test_norm_currency(value, expected):
    assert norm_currency(value) == expected


def test_confirmed_same_requires_both_known_and_equal():
    assert currencies_confirmed_same("USD", "USD") is True
    assert currencies_confirmed_same("USD", "INR") is False
    assert currencies_confirmed_same("USD", None) is False   # unknown -> not confirmed
    assert currencies_confirmed_same(None, None) is False


def test_incompatible_is_only_known_and_different():
    assert currencies_incompatible("USD", "INR") is True
    assert currencies_incompatible("USD", "USD") is False
    assert currencies_incompatible("USD", None) is False     # unknown -> not proven
    assert currencies_incompatible(None, None) is False


def test_cross_currency_ratio_computes_only_when_confirmed_same():
    val, blocked = cross_currency_ratio(50.0, 200.0, num_ccy="USD", den_ccy="USD",
                                        scale=100.0)
    assert val == 25.0 and blocked is False


def test_cross_currency_ratio_blocks_on_mismatch():
    val, blocked = cross_currency_ratio(50.0, 200.0, num_ccy="INR", den_ccy="USD")
    assert val is None and blocked is True               # explicit currency block


def test_cross_currency_ratio_blocks_on_unknown_currency():
    val, blocked = cross_currency_ratio(50.0, 200.0, num_ccy="USD", den_ccy=None)
    assert val is None and blocked is True               # can't confirm -> fail closed


def test_cross_currency_ratio_missing_input_is_absence_not_block():
    assert cross_currency_ratio(None, 200.0, num_ccy="USD", den_ccy="USD") == (None, False)
    assert cross_currency_ratio(50.0, 0.0, num_ccy="USD", den_ccy="USD") == (None, False)
    assert cross_currency_ratio(50.0, None, num_ccy="USD", den_ccy="USD") == (None, False)


# ------------------------------------------------------------- conformance

_RESEARCH = Path(__file__).resolve().parents[2] / "atlas" / "dcp" / "research"


@pytest.mark.parametrize("module", [
    "financials_panel.py", "valuation_models.py", "health_score.py",
])
def test_ratio_modules_import_the_shared_layer(module):
    """The three modules that form a statement/market ratio must obtain the
    currency rule from ratios.py — not re-derive `str(x).upper() == str(y)...`
    inline (the pattern that let one of them drift)."""
    src = (_RESEARCH / module).read_text()
    assert "from atlas.dcp.research.ratios import" in src


def test_no_inline_currency_equality_reimplementation():
    """Guard against a reintroduced inline `reporting == listing` currency check
    in the ratio modules (it must go through currencies_confirmed_same/
    _incompatible in ratios.py)."""
    for module in ("financials_panel.py", "valuation_models.py", "health_score.py"):
        src = (_RESEARCH / module).read_text()
        assert "reporting_cur != listing_cur" not in src
        assert "str(reporting_cur) == str(listing_cur)" not in src
