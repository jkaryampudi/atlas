"""F-001 adversarial tests for the point-in-time membership / era guard.

series_overlaps_membership() is the fail-closed gate that keeps wrong-era and
reused-ticker (wrong-issuer) price series out of the definitive validation panel.
Each scenario below is one of the review's adversarial cases; the guard has no
issuer identity (F-002), so it resolves only what is unambiguous from the
membership interval and fails closed on the rest.
"""
from __future__ import annotations

from datetime import date

from atlas.dcp.market_data.index_membership import (
    MembershipRow,
    is_member_on,
    series_overlaps_membership,
)


def _row(ticker="ACME", start=date(2015, 1, 1), end=date(2020, 1, 1),
         active=False, delisted=True) -> MembershipRow:
    return MembershipRow(index_code="GSPC.INDX", ticker=ticker, name=ticker,
                         start_date=start, end_date=end,
                         is_active_now=active, is_delisted=delisted)


def _days(a: date, b: date, step: int = 30):
    out, d = [], a
    from datetime import timedelta
    while d <= b:
        out.append(d)
        d += timedelta(days=step)
    return out


# 1. member with in-era bars -> admitted
def test_in_era_series_is_admitted():
    row = _row(start=date(2015, 1, 1), end=date(2020, 1, 1))
    assert series_overlaps_membership(row, _days(date(2015, 6, 1), date(2019, 6, 1)))


# 2/3. reused ticker: series begins entirely AFTER end_date -> refused (the
# ADT/VAL/MNK case: a different company under the same code)
def test_reused_ticker_series_after_end_is_refused():
    row = _row(start=date(2005, 1, 1), end=date(2015, 8, 1))
    later = _days(date(2018, 1, 1), date(2024, 1, 1))     # a different company
    assert series_overlaps_membership(row, later) is False


# 4. series ends entirely BEFORE start_date -> refused (wrong era)
def test_series_entirely_before_start_is_refused():
    row = _row(start=date(2015, 1, 1), end=date(2020, 1, 1))
    earlier = _days(date(2008, 1, 1), date(2012, 1, 1))
    assert series_overlaps_membership(row, earlier) is False


# 5. delisting then reuse: bars straddle end_date but include in-era bars ->
# admitted (has genuine member-era coverage; the post-era tail is bounded by
# is_member_on at ranking time)
def test_straddling_series_with_in_era_bars_is_admitted():
    row = _row(start=date(2015, 1, 1), end=date(2018, 1, 1))
    straddle = _days(date(2016, 1, 1), date(2020, 1, 1))
    assert series_overlaps_membership(row, straddle)


# 6. two membership spells cannot be represented by one row; only the row's own
# interval is honoured (documented limitation) — a bar only in the GAP between
# two spells is not admitted by this single-interval row
def test_single_interval_row_honours_only_its_own_span():
    row = _row(start=date(2015, 1, 1), end=date(2016, 1, 1))   # first spell only
    gap_only = _days(date(2017, 1, 1), date(2018, 1, 1))       # between spells
    assert series_overlaps_membership(row, gap_only) is False


# 7. unusable row (null start, not current) -> never admits anything (fail closed)
def test_unusable_row_admits_nothing():
    row = MembershipRow(index_code="GSPC.INDX", ticker="ALTR", name="ALTR", start_date=None, end_date=date(2015, 1, 1),
                        is_active_now=False, is_delisted=True)
    assert series_overlaps_membership(row, _days(date(2010, 1, 1), date(2014, 1, 1))) is False


# 8. current member with null start -> usable from window start; any bar up to
# end (null end = still a member) is admitted
def test_current_member_null_start_is_usable():
    row = MembershipRow(index_code="GSPC.INDX", ticker="AAPL", name="AAPL", start_date=None, end_date=None,
                        is_active_now=True, is_delisted=False)
    assert series_overlaps_membership(row, _days(date(2015, 1, 1), date(2024, 1, 1)))


# 9. end-exclusive boundary: a single bar exactly ON end_date is NOT in-era
def test_end_date_is_exclusive():
    row = _row(start=date(2015, 1, 1), end=date(2020, 1, 1))
    assert is_member_on(row, date(2020, 1, 1)) is False
    assert series_overlaps_membership(row, [date(2020, 1, 1)]) is False
    assert series_overlaps_membership(row, [date(2019, 12, 31)])


# 10. empty series -> refused (nothing to admit)
def test_empty_series_is_refused():
    assert series_overlaps_membership(_row(), []) is False
