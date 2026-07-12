"""Total-return builder (board memo 2026-07 item 1) — hand-pinned math.

The convention under test (total_return.py module docstring): each dividend is
reinvested at its EX-DATE'S CLOSE via a cumulative factor multiplying opens
and closes alike, so intraday relative moves never change and the overnight
ex-date gap absorbs the compensation. Dividends split-adjust with the SAME
strictly-before rule as bars. Nothing is silently discarded: out-of-series
ex-dates are dropped AND counted; off-calendar ex-dates roll forward, counted.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from atlas.dcp.market_data.models import Dividend, Split
from atlas.dcp.market_data.total_return import (
    adjust_dividends_for_splits,
    total_return_series,
)

D0, D1, D2, D3 = (date(2024, 1, 2), date(2024, 1, 3),
                  date(2024, 1, 4), date(2024, 1, 5))


def _div(ex: date, amount: str, symbol: str = "AAA") -> Dividend:
    return Dividend(symbol=symbol, ex_date=ex, amount=Decimal(amount))


def test_single_dividend_hand_pinned():
    """One $2.02 dividend, ex-date D2 whose close is 101: factor 1.02 from D2
    on. Every number below is computed by hand, not by the code under test."""
    dates = [D0, D1, D2, D3]
    opens = [99.0, 101.5, 100.0, 102.5]
    closes = [100.0, 102.0, 101.0, 103.0]
    trs = total_return_series(dates=dates, opens=opens, closes=closes,
                              dividends=[_div(D2, "2.02")])
    assert trs.applied == 1
    assert (trs.dropped_before, trs.dropped_after, trs.rolled) == (0, 0, 0)
    # factor: 1.0, 1.0, then 1 + 2.02/101 = 1.02
    assert trs.closes == pytest.approx([100.0, 102.0, 103.02, 105.06])
    assert trs.opens == pytest.approx([99.0, 101.5, 102.0, 104.55])


def test_tr_return_across_ex_date_is_price_plus_dividend():
    """The economic identity: the close-to-close TR growth across the ex-date
    equals (C_ex + D) / C_prev — a holder is credited the cash."""
    dates = [D0, D1]
    trs = total_return_series(dates=dates, opens=[100.0, 95.0],
                              closes=[100.0, 96.0],
                              dividends=[_div(D1, "5")])
    assert trs.closes[1] / trs.closes[0] == pytest.approx((96.0 + 5.0) / 100.0)


def test_intraday_and_post_ex_returns_unchanged():
    """Opens and closes share one factor: open->close on the ex-date and every
    return after it are identical to the price series (a buyer at the ex-date
    open gets NO credit for the dividend)."""
    dates = [D0, D1, D2, D3]
    opens = [99.0, 101.5, 100.0, 102.5]
    closes = [100.0, 102.0, 101.0, 103.0]
    trs = total_return_series(dates=dates, opens=opens, closes=closes,
                              dividends=[_div(D1, "3")])
    for i in range(4):
        assert trs.closes[i] / trs.opens[i] == pytest.approx(closes[i] / opens[i])
    assert trs.closes[3] / trs.opens[2] == pytest.approx(closes[3] / opens[2])


def test_two_dividends_compound():
    dates = [D0, D1, D2]
    closes = [100.0, 100.0, 100.0]
    trs = total_return_series(dates=dates, opens=list(closes),
                              closes=list(closes),
                              dividends=[_div(D1, "1"), _div(D2, "1")])
    # factors: 1, 1.01, 1.01 * 1.01
    assert trs.closes == pytest.approx([100.0, 101.0, 102.01])


def test_split_dividend_interaction_hand_pinned():
    """A $1 dividend declared before a 4-for-1 split adjusts to $0.25 (the
    strictly-before bar rule applied to cash); one after it is untouched;
    two splits compound."""
    split4 = Split(symbol="AAA", action_date=date(2024, 6, 10), ratio=Decimal(4))
    split2 = Split(symbol="AAA", action_date=date(2024, 9, 10), ratio=Decimal(2))
    before_both = _div(date(2024, 3, 1), "1")
    between = _div(date(2024, 7, 1), "1")
    after_both = _div(date(2024, 10, 1), "1")
    on_split_day = _div(date(2024, 6, 10), "1")   # NOT strictly before
    adj = adjust_dividends_for_splits(
        [before_both, between, after_both, on_split_day], [split4, split2])
    amounts = {d.ex_date: d.amount for d in adj}
    assert amounts[date(2024, 3, 1)] == Decimal("0.125")   # / (4*2)
    assert amounts[date(2024, 7, 1)] == Decimal("0.5")     # / 2
    assert amounts[date(2024, 10, 1)] == Decimal("1")
    # ON the split day = NOT strictly before it -> only the later 2:1 applies
    assert amounts[date(2024, 6, 10)] == Decimal("0.5")


def test_split_adjustment_ignores_other_symbols_and_no_splits_is_identity():
    d = _div(date(2024, 3, 1), "1", symbol="AAA")
    other = Split(symbol="BBB", action_date=date(2024, 6, 10), ratio=Decimal(4))
    assert adjust_dividends_for_splits([d], [other])[0].amount == Decimal("1")
    assert adjust_dividends_for_splits([d], []) == [d]


def test_div_over_close_invariant_to_adjustment_basis():
    """D/C — hence the TR factor — is the same whether computed on raw prices
    with the raw dividend or on split-adjusted prices with the adjusted
    dividend: the split factor cancels by construction."""
    raw_close = 400.0
    raw_div = Decimal("2")
    ratio = Decimal(4)
    adj_close = raw_close / float(ratio)
    adj_div = raw_div / ratio
    assert float(raw_div) / raw_close == pytest.approx(
        float(adj_div) / adj_close)


def test_out_of_series_dividends_dropped_and_counted():
    """Before inception: unreinvestable (no position could hold it). After the
    final bar: the delisting rule already converted the position to cash at
    its final close. Both DROPPED, both COUNTED, series untouched."""
    dates = [D1, D2]
    closes = [100.0, 101.0]
    trs = total_return_series(
        dates=dates, opens=list(closes), closes=list(closes),
        dividends=[_div(D0, "9"), _div(date(2024, 2, 1), "9")])
    assert trs.applied == 0
    assert trs.dropped_before == 1
    assert trs.dropped_after == 1
    assert trs.closes == pytest.approx(closes)


def test_off_calendar_ex_date_rolls_forward_to_next_session():
    """An ex-date with no bar (vendor quirk) reinvests at the NEXT session's
    close and is counted as rolled — never silently discarded."""
    dates = [D0, D1, D3]                       # D2 missing
    closes = [100.0, 102.0, 104.0]
    trs = total_return_series(dates=dates, opens=list(closes),
                              closes=list(closes),
                              dividends=[_div(D2, "5.2")])
    assert trs.applied == 1 and trs.rolled == 1
    # factor at D3 = 1 + 5.2/104 = 1.05
    assert trs.closes == pytest.approx([100.0, 102.0, 109.2])


def test_zero_dividend_symbol_is_identity():
    dates = [D0, D1]
    closes = [100.0, 101.0]
    opens = [99.5, 100.5]
    trs = total_return_series(dates=dates, opens=opens, closes=closes,
                              dividends=[])
    assert trs.opens == opens and trs.closes == closes
    assert trs.applied == 0


def test_length_mismatch_refused():
    with pytest.raises(ValueError, match="length mismatch"):
        total_return_series(dates=[D0, D1], opens=[1.0], closes=[1.0, 2.0],
                            dividends=[])


def test_non_positive_dividend_refused_by_model():
    with pytest.raises(ValueError, match="non-positive dividend"):
        Dividend(symbol="AAA", ex_date=D0, amount=Decimal("0"))
    with pytest.raises(ValueError, match="non-positive dividend"):
        Dividend(symbol="AAA", ex_date=D0, amount=Decimal("-1"))
