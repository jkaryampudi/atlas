"""Session-close guard for the daily cycle (atlas/ops/daily.py cycle_refusal).

Production defect 2026-07-13: a console click at 11:07 AEST (01:07 UTC)
created and completed daily-2026-07-13 before Monday's US session had traded;
the evening scheduler then replayed the finished checkpoint and the day ran
on Friday's closes. The guard refuses to start a US-session date's cycle
until that session's close plus the pinned vendor grace — structurally
(calendar + injected clock), before the checkpoint row exists.

Pure tests, no DB: the pg suite (tests/integration/test_daily_cycle_pg.py)
proves the no-checkpoint-row and fresh-rerun properties.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.calendars import session_close_utc, trading_days_between
from atlas.ops.daily import (
    CYCLE_EARLIEST_AFTER_CLOSE_MIN,
    EXIT_REFUSED,
    cycle_refusal,
)
from atlas.ops.scheduler import CYCLE_UTC

REFUSAL_2026_07_13 = ("cycle for 2026-07-13 refused: US session not yet closed "
                      "(closes 20:00 UTC + 30min vendor grace); "
                      "re-run after 20:30 UTC")


def test_mid_session_click_is_refused_with_the_pinned_message():
    """The exact production instant: 11:07 AEST = 01:07 UTC, Monday 2026-07-13,
    18h53m before the XNYS close."""
    clock = FrozenClock(datetime(2026, 7, 13, 1, 7, tzinfo=UTC))
    assert cycle_refusal(clock) == REFUSAL_2026_07_13


def test_just_after_the_bell_is_still_refused_inside_the_grace():
    """20:10 UTC is after the close but inside the 30min publication grace —
    the 'click right at the close' edge of the same defect."""
    clock = FrozenClock(datetime(2026, 7, 13, 20, 10, tzinfo=UTC))
    assert cycle_refusal(clock) == REFUSAL_2026_07_13


def test_exactly_at_close_plus_grace_proceeds():
    clock = FrozenClock(datetime(2026, 7, 13, 20, 30, tzinfo=UTC))
    assert cycle_refusal(clock) is None


def test_scheduled_2330_utc_firing_passes_for_its_target_date():
    clock = FrozenClock(datetime(2026, 7, 13, 23, 30, tzinfo=UTC))
    assert cycle_refusal(clock) is None


def test_weekend_passes_through_at_any_hour():
    """Non-session days keep their existing behavior (the cycle runs and
    records 'not a US session') — even at the defect's mid-morning instant."""
    assert cycle_refusal(FrozenClock(datetime(2026, 7, 12, 1, 7, tzinfo=UTC))) is None
    assert cycle_refusal(FrozenClock(datetime(2026, 7, 12, 23, 30, tzinfo=UTC))) is None


def test_holiday_passes_through():
    """2026-07-03 (Independence Day observed) is an XNYS holiday: not a
    session, so the guard stays out of the way mid-morning."""
    assert cycle_refusal(FrozenClock(datetime(2026, 7, 3, 15, 0, tzinfo=UTC))) is None


def test_winter_session_uses_the_calendar_close_not_a_constant():
    """XNYS closes 21:00 UTC under EST: the refusal must quote the calendar's
    close for the date, never a hard-coded 20:00."""
    clock = FrozenClock(datetime(2026, 1, 13, 21, 15, tzinfo=UTC))
    assert cycle_refusal(clock) == (
        "cycle for 2026-01-13 refused: US session not yet closed "
        "(closes 21:00 UTC + 30min vendor grace); re-run after 21:30 UTC")


def test_early_close_half_day_is_refused_until_its_own_close():
    """Black Friday 2026-11-27 closes 18:00 UTC (13:00 EST half-day): refused
    at 17:00 UTC, clear by 18:30 UTC — early closes come from the calendar."""
    assert cycle_refusal(FrozenClock(datetime(2026, 11, 27, 17, 0, tzinfo=UTC))) == (
        "cycle for 2026-11-27 refused: US session not yet closed "
        "(closes 18:00 UTC + 30min vendor grace); re-run after 18:30 UTC")
    assert cycle_refusal(
        FrozenClock(datetime(2026, 11, 27, 18, 30, tzinfo=UTC))) is None


def test_scheduled_fire_always_passes_the_guard():
    """PROVABLY unaffected scheduled path: for every XNYS session across a
    full calendar year (DST shifts, half-days, all of it), the 23:30 UTC
    firing lands at or after close + grace, so the scheduler's own run can
    never be refused. The latest close all year is 21:00 UTC (winter)."""
    grace = timedelta(minutes=CYCLE_EARLIEST_AFTER_CLOSE_MIN)
    sessions = trading_days_between("US", datetime(2026, 1, 1, tzinfo=UTC).date(),
                                    datetime(2026, 12, 31, tzinfo=UTC).date())
    assert len(sessions) > 240  # a real year of sessions, not an empty range
    for day in sessions:
        fire = datetime.combine(day, CYCLE_UTC, tzinfo=UTC)
        assert session_close_utc("US", day) + grace <= fire, day
        assert cycle_refusal(FrozenClock(fire)) is None, day


def test_exit_code_is_distinct_from_clean_and_failed():
    """0 = clean day, 2 = node/ingest failure, EXIT_REFUSED = come back after
    the close: launchd/cron logs must be able to tell all three apart."""
    assert EXIT_REFUSED not in (0, 2)
