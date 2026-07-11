"""Nightly incremental window + FX completion math (pure — no DB, no vendor).

The completed-session convention is the whole safety story here: a session
still in progress must never enter a fetch window, or a partial bar becomes
look-ahead poison for anything replaying "as of" that day.
"""
from datetime import UTC, date, datetime

import pytest

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.daily import fx_last_completed_weekday, incremental_sessions


def test_window_mid_session_excludes_in_progress_day():
    # Monday 2024-07-15 15:00 UTC = 11:00 ET — XNYS is open; Monday is excluded.
    clock = FrozenClock(datetime(2024, 7, 15, 15, 0, tzinfo=UTC))
    assert incremental_sessions("US", date(2024, 7, 11), clock.now()) == [date(2024, 7, 12)]


def test_window_after_close_includes_the_day():
    # 21:00 UTC is past the 20:00 UTC close: Monday is now a completed session.
    clock = FrozenClock(datetime(2024, 7, 15, 21, 0, tzinfo=UTC))
    assert incremental_sessions("US", date(2024, 7, 11), clock.now()) == [
        date(2024, 7, 12), date(2024, 7, 15)]


def test_window_empty_when_up_to_date():
    clock = FrozenClock(datetime(2024, 7, 15, 21, 0, tzinfo=UTC))
    assert incremental_sessions("US", date(2024, 7, 15), clock.now()) == []


def test_window_skips_weekend_and_holiday():
    # 2025-07-04 (Friday) is Independence Day: the window jumps 07-03 -> 07-07.
    clock = FrozenClock(datetime(2025, 7, 7, 21, 0, tzinfo=UTC))
    assert incremental_sessions("US", date(2025, 7, 2), clock.now()) == [
        date(2025, 7, 3), date(2025, 7, 7)]


def test_window_au_mid_session_vs_after_close():
    # Sydney winter close is 06:00 UTC: 05:00 UTC is mid-session, 06:00 is done.
    mid = FrozenClock(datetime(2024, 7, 15, 5, 0, tzinfo=UTC))
    assert incremental_sessions("AU", date(2024, 7, 11), mid.now()) == [date(2024, 7, 12)]
    closed = FrozenClock(datetime(2024, 7, 15, 6, 0, tzinfo=UTC))
    assert incremental_sessions("AU", date(2024, 7, 11), closed.now()) == [
        date(2024, 7, 12), date(2024, 7, 15)]


def test_fx_weekday_completes_at_2200_utc():
    assert fx_last_completed_weekday(
        datetime(2024, 7, 15, 21, 59, tzinfo=UTC)) == date(2024, 7, 12)
    assert fx_last_completed_weekday(
        datetime(2024, 7, 15, 22, 0, tzinfo=UTC)) == date(2024, 7, 15)


def test_fx_weekend_rolls_back_to_friday():
    assert fx_last_completed_weekday(
        datetime(2024, 7, 13, 12, 0, tzinfo=UTC)) == date(2024, 7, 12)
    assert fx_last_completed_weekday(
        datetime(2024, 7, 14, 23, 0, tzinfo=UTC)) == date(2024, 7, 12)


def test_fx_requires_aware_datetime():
    with pytest.raises(ValueError, match="aware"):
        fx_last_completed_weekday(datetime(2024, 7, 15, 22, 0))
