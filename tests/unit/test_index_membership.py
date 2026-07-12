"""Membership-interval rule (fail-closed) — the single source of truth for the
point-in-time S&P 500 reconstruction. Every null/date combination is pinned,
including the fail-closed exclusions: a null StartDate is usable ONLY for a
current member; null-start delisted AND null-start departed rows are excluded
entirely (unknowable interval — and demonstrably ticker-reuse-confused)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from atlas.dcp.market_data.index_membership import (
    MembershipRow,
    is_member_on,
    member_in_window,
    parse_membership,
    partition_membership,
    usable,
    write_member_seeds_csv,
)

D = date(2015, 6, 30)


def row(start: date | None, end: date | None, *, active: bool = False,
        delisted: bool = False, ticker: str = "TST") -> MembershipRow:
    return MembershipRow(index_code="GSPC.INDX", ticker=ticker, name="Test Co",
                         start_date=start, end_date=end, is_active_now=active,
                         is_delisted=delisted)


# --- the four start/end null combinations -----------------------------------

def test_both_dates_present_member_inside_interval():
    r = row(date(2014, 1, 2), date(2016, 1, 2))
    assert is_member_on(r, D)
    assert not is_member_on(r, date(2013, 12, 31))     # before start
    assert not is_member_on(r, date(2016, 6, 1))       # after end


def test_start_present_end_null_member_from_start_onward():
    r = row(date(2014, 1, 2), None, active=True)
    assert is_member_on(r, D)
    assert is_member_on(r, date(2099, 1, 1))
    assert not is_member_on(r, date(2013, 12, 31))


def test_null_start_with_end_active_member_until_end():
    """Rule stated verbatim: (start IS NULL OR start <= D) AND (end IS NULL OR
    end > D) — over a USABLE row. Active-now makes a null start usable."""
    r = row(None, date(2016, 1, 2), active=True)
    assert is_member_on(r, D)                          # any D before end
    assert is_member_on(r, date(1957, 3, 4))
    assert not is_member_on(r, date(2016, 6, 1))


def test_null_start_null_end_active_member_always():
    r = row(None, None, active=True)
    assert usable(r)
    assert is_member_on(r, date(1957, 3, 4))
    assert is_member_on(r, date(2099, 1, 1))


# --- interval boundary semantics ---------------------------------------------

def test_member_on_start_date_but_not_on_end_date():
    """start-inclusive, end-EXCLUSIVE: on its removal date a ticker is no
    longer a member."""
    r = row(date(2014, 1, 2), date(2016, 1, 2))
    assert is_member_on(r, date(2014, 1, 2))
    assert not is_member_on(r, date(2016, 1, 2))
    assert is_member_on(r, date(2016, 1, 1))


# --- fail-closed exclusions ---------------------------------------------------

def test_null_start_delisted_excluded_entirely():
    r = row(None, date(2015, 12, 29), delisted=True)
    assert not usable(r)
    assert not is_member_on(r, D)                      # even inside "interval"
    assert not is_member_on(r, date(2015, 12, 28))
    assert not member_in_window(r, date(2012, 7, 1), date(2026, 7, 10))


def test_null_start_departed_not_delisted_excluded_entirely():
    """Neither active nor delisted with a null start: the interval is just as
    unknowable — the rule's ONLY-when-active branch fails closed here too."""
    r = row(None, date(2018, 12, 3))
    assert not usable(r)
    assert not is_member_on(r, D)
    assert not member_in_window(r, date(2012, 7, 1), date(2026, 7, 10))


def test_partition_counts_all_three_buckets():
    rows = [row(date(2014, 1, 2), None, active=True, ticker="OK1"),
            row(None, None, active=True, ticker="OK2"),
            row(None, date(2015, 1, 2), delisted=True, ticker="EXDL"),
            row(None, date(2015, 1, 2), ticker="EXDEP")]
    p = partition_membership(rows)
    assert [r.ticker for r in p.usable] == ["OK1", "OK2"]
    assert [r.ticker for r in p.excluded_null_start_delisted] == ["EXDL"]
    assert [r.ticker for r in p.excluded_null_start_departed] == ["EXDEP"]


# --- window intersection (needed-ticker computation) --------------------------

def test_member_in_window_end_exclusive_and_start_inclusive():
    w0, w1 = date(2012, 7, 1), date(2026, 7, 10)
    assert not member_in_window(row(date(2000, 1, 3), date(2012, 7, 1)), w0, w1)
    assert member_in_window(row(date(2000, 1, 3), date(2012, 7, 2)), w0, w1)
    assert member_in_window(row(date(2026, 7, 10), None, active=True), w0, w1)
    assert not member_in_window(row(date(2026, 7, 11), None, active=True), w0, w1)
    assert member_in_window(row(None, None, active=True), w0, w1)


# --- vendor payload parsing ----------------------------------------------------

PAYLOAD = {
    "General": {"Code": "GSPC"},
    "HistoricalTickerComponents": {
        "0": {"Code": "AAA", "Name": "Alpha", "StartDate": "2000-06-05",
              "EndDate": None, "IsActiveNow": 1, "IsDelisted": 0},
        "1": {"Code": "BBB", "Name": "Beta", "StartDate": None,
              "EndDate": "2015-12-29", "IsActiveNow": 0, "IsDelisted": 1},
    },
}


def test_parse_membership_keeps_nulls_verbatim():
    rows = parse_membership(PAYLOAD, index_code="GSPC.INDX")
    assert [r.ticker for r in rows] == ["AAA", "BBB"]
    a, b = rows
    assert a.start_date == date(2000, 6, 5) and a.end_date is None
    assert a.is_active_now and not a.is_delisted
    assert b.start_date is None and b.end_date == date(2015, 12, 29)
    assert not b.is_active_now and b.is_delisted


def test_parse_membership_accepts_list_form():
    payload = {"HistoricalTickerComponents":
               list(PAYLOAD["HistoricalTickerComponents"].values())}
    assert len(parse_membership(payload)) == 2


def test_parse_membership_refuses_missing_table():
    with pytest.raises(ValueError, match="no HistoricalTickerComponents"):
        parse_membership({"General": {}, "Components": {}})


def test_parse_membership_refuses_empty_table():
    with pytest.raises(ValueError, match="empty"):
        parse_membership({"HistoricalTickerComponents": {}})


def test_parse_membership_refuses_duplicate_codes():
    payload = {"HistoricalTickerComponents": [
        {"Code": "AAA", "StartDate": "2000-06-05", "EndDate": None,
         "IsActiveNow": 1, "IsDelisted": 0},
        {"Code": "AAA", "StartDate": "2010-06-05", "EndDate": None,
         "IsActiveNow": 1, "IsDelisted": 0}]}
    with pytest.raises(ValueError, match="duplicate ticker code"):
        parse_membership(payload)


def test_seeds_csv_shape(tmp_path: Path):
    rows = [row(date(2014, 1, 2), None, active=True, ticker="ZZZ"),
            row(date(2010, 1, 4), date(2020, 1, 2), delisted=True, ticker="AAA")]
    path = tmp_path / "members.csv"
    assert write_member_seeds_csv(rows, path) == 2
    lines = path.read_text().strip().splitlines()
    assert lines[0] == ("symbol,exchange,market,instrument_type,name,"
                        "sector_gics,currency,economic_exposure")
    assert lines[1].startswith("AAA,US,US,stock,")   # sorted by ticker
    assert lines[2].startswith("ZZZ,US,US,stock,")
