"""Exchange trading calendars (task 1a): XNYS for US, XASX for AU.

Golden pins against known holidays/weekends — these replace the naive
weekend-skip, so the pins deliberately include a holiday a weekend-skip
would get wrong.
"""
from datetime import UTC, date, datetime

import pytest

from atlas.dcp.market_data.calendars import (
    is_trading_day,
    last_completed_session,
    local_date,
    previous_trading_day,
    recent_sessions,
    session_close_utc,
    trading_days_between,
)


def test_us_weekday_is_trading_day():
    assert is_trading_day("US", date(2024, 7, 15))  # Monday


def test_us_weekend_is_not_trading_day():
    assert not is_trading_day("US", date(2024, 7, 13))  # Saturday


def test_us_holiday_is_not_trading_day():
    # Independence Day 2025 falls on a Friday — a weekend-skip would miss this.
    assert not is_trading_day("US", date(2025, 7, 4))


def test_previous_trading_day_over_weekend():
    assert previous_trading_day("US", date(2024, 7, 15)) == date(2024, 7, 12)


def test_previous_trading_day_over_holiday_weekend():
    # Monday 2025-07-07: previous session skips the weekend AND July 4.
    assert previous_trading_day("US", date(2025, 7, 7)) == date(2025, 7, 3)


def test_trading_days_between_golden_week():
    assert trading_days_between("US", date(2024, 7, 10), date(2024, 7, 15)) == [
        date(2024, 7, 10),
        date(2024, 7, 11),
        date(2024, 7, 12),
        date(2024, 7, 15),
    ]


def test_trading_days_between_empty_when_inverted():
    assert trading_days_between("US", date(2024, 7, 15), date(2024, 7, 10)) == []


def test_recent_sessions_includes_trading_day_itself():
    assert recent_sessions("US", date(2024, 7, 15), lookback=1) == [
        date(2024, 7, 12),
        date(2024, 7, 15),
    ]


def test_recent_sessions_on_non_trading_day_excludes_it():
    # Saturday: the day itself is not expected; lookback still counts sessions.
    assert recent_sessions("US", date(2024, 7, 13), lookback=1) == [date(2024, 7, 12)]


def test_recent_sessions_zero_lookback():
    assert recent_sessions("US", date(2024, 7, 15), lookback=0) == [date(2024, 7, 15)]


def test_au_australia_day_is_not_trading_day():
    assert not is_trading_day("AU", date(2025, 1, 27))  # Australia Day (observed), Monday


def test_au_weekday_is_trading_day():
    assert is_trading_day("AU", date(2025, 1, 28))


def test_unknown_market_raises():
    with pytest.raises(ValueError, match="no exchange calendar"):
        is_trading_day("MARS", date(2024, 7, 15))


def test_out_of_bounds_date_raises_clearly():
    # Bounds are fixed literals, not wall-clock-relative (review finding):
    # the same query must behave identically regardless of the day it runs.
    with pytest.raises(ValueError, match="outside supported calendar bounds"):
        is_trading_day("US", date(2031, 1, 2))
    with pytest.raises(ValueError, match="outside supported calendar bounds"):
        previous_trading_day("US", date(2005, 6, 1))


def test_early_bound_is_deterministic():
    # 2007 is inside the fixed bounds even though it is >20y before some future
    # run date — the default (today-20y) window would eventually break this.
    days = trading_days_between("US", date(2007, 1, 3), date(2007, 1, 5))
    assert days == [date(2007, 1, 3), date(2007, 1, 4), date(2007, 1, 5)]


def test_session_close_utc_golden_pins():
    # XNYS regular day: 16:00 ET = 20:00 UTC in July (EDT).
    assert session_close_utc("US", date(2024, 7, 15)) == datetime(2024, 7, 15, 20, 0,
                                                                  tzinfo=UTC)
    # XASX: 16:00 Sydney = 06:00 UTC in July (AEST).
    assert session_close_utc("AU", date(2024, 7, 15)) == datetime(2024, 7, 15, 6, 0,
                                                                  tzinfo=UTC)


def test_session_close_utc_half_day_comes_from_calendar():
    # Black Friday 2024: XNYS closes early at 13:00 EST = 18:00 UTC — a
    # hardcoded 20:00 would call the afternoon a data hole.
    assert session_close_utc("US", date(2024, 11, 29)) == datetime(2024, 11, 29, 18, 0,
                                                                   tzinfo=UTC)


def test_last_completed_session_mid_session_is_previous():
    # Monday 15:00 UTC = 11:00 ET, XNYS open: Monday has not completed.
    at = datetime(2024, 7, 15, 15, 0, tzinfo=UTC)
    assert last_completed_session("US", at) == date(2024, 7, 12)


def test_last_completed_session_at_close_is_completed():
    at = datetime(2024, 7, 15, 20, 0, tzinfo=UTC)
    assert last_completed_session("US", at) == date(2024, 7, 15)


def test_last_completed_session_weekend():
    at = datetime(2024, 7, 13, 4, 0, tzinfo=UTC)  # Saturday
    assert last_completed_session("US", at) == date(2024, 7, 12)


def test_last_completed_session_au_east_of_utc():
    # At Sunday 22:00 UTC Sydney is already Monday 08:00 (pre-open): Friday is
    # still the last completed session; Monday completes at its 06:00 UTC close.
    assert last_completed_session(
        "AU", datetime(2024, 7, 14, 22, 0, tzinfo=UTC)) == date(2024, 7, 12)
    assert last_completed_session(
        "AU", datetime(2024, 7, 15, 6, 0, tzinfo=UTC)) == date(2024, 7, 15)


def test_last_completed_session_requires_aware_datetime():
    with pytest.raises(ValueError, match="aware"):
        last_completed_session("US", datetime(2024, 7, 15, 15, 0))


def test_local_date_uses_exchange_timezone():
    at = datetime(2024, 7, 14, 22, 0, tzinfo=UTC)
    assert local_date("AU", at) == date(2024, 7, 15)  # Sydney is past midnight
    assert local_date("US", at) == date(2024, 7, 14)  # New York is not
    with pytest.raises(ValueError, match="aware"):
        local_date("US", datetime(2024, 7, 14, 22, 0))
