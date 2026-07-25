"""F-010: the free-cash-flow YIELD divides statement-currency FCF by the listing
(price) currency market cap. For a non-USD-reporting ADR that is a currency mix
and must fail closed, while the margin ratios (statement/statement) still compute."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from atlas.dcp.research.health_score import _factors_from_row


def _row(**kw):
    base = dict(mcap="1000", rev="500", gp="200", fcf="100", pe=None, evebitda=None,
                pb=None, ps=None, roe=None, om=None, pm=None, rg=None, eg=None,
                reporting_cur="USD", listing_cur="USD")
    base.update(kw)
    return SimpleNamespace(**base)


def test_matching_currency_computes_fcf_yield():
    f = _factors_from_row(_row(reporting_cur="USD", listing_cur="USD"))
    assert f["fcf_yield"] == pytest.approx(100 / 1000)


def test_mismatched_currency_fails_closed_but_margins_survive():
    f = _factors_from_row(_row(reporting_cur="EUR", listing_cur="USD"))
    assert f["fcf_yield"] is None                       # currency-mixed -> excluded
    assert f["fcf_margin"] == pytest.approx(100 / 500)  # statement/statement -> kept
    assert f["gross_margin"] == pytest.approx(200 / 500)


def test_unknown_reporting_currency_fails_closed():
    assert _factors_from_row(_row(reporting_cur=None))["fcf_yield"] is None
