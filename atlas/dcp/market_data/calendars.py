"""Exchange trading calendars (Phase 1 checklist item).

Wraps `exchange_calendars` so the rest of the plane speaks plain `date`s:
US -> XNYS, AU -> XASX. Replaces the naive weekend-skip — holidays are
non-trading days, and gates must never demand bars for them.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

import exchange_calendars as xcals

MARKET_CALENDARS = {"US": "XNYS", "AU": "XASX"}

# Deterministic calendar bounds. exchange_calendars' defaults are wall-clock
# relative (today-20y .. today+1y) and lru_cache would pin whatever the run day
# was — the same query could work today and crash tomorrow (review finding;
# injectable-time invariant). Fixed bounds are bumped deliberately, in review.
CALENDAR_START = date(2006, 1, 3)
CALENDAR_END = date(2030, 12, 31)


@lru_cache(maxsize=None)
def _calendar(market: str) -> Any:
    try:
        code = MARKET_CALENDARS[market]
    except KeyError:
        raise ValueError(f"no exchange calendar mapped for market {market!r}") from None
    return xcals.get_calendar(code, start=CALENDAR_START.isoformat(),
                              end=CALENDAR_END.isoformat())


def _check_bounds(day: date) -> None:
    if not (CALENDAR_START <= day <= CALENDAR_END):
        raise ValueError(f"{day} outside supported calendar bounds "
                         f"[{CALENDAR_START}, {CALENDAR_END}] — bump CALENDAR_END "
                         "deliberately if the horizon moved")


def is_trading_day(market: str, day: date) -> bool:
    _check_bounds(day)
    return bool(_calendar(market).is_session(day.isoformat()))


def previous_trading_day(market: str, day: date) -> date:
    """Latest session strictly before `day` (skips weekends AND holidays)."""
    _check_bounds(day)
    ts = _calendar(market).date_to_session(
        (day - timedelta(days=1)).isoformat(), direction="previous")
    prev: date = ts.date()
    return prev


def next_trading_day(market: str, day: date) -> date:
    """Earliest session strictly after `day` (skips weekends AND holidays).
    The paper broker fills at THIS session's open (Phase 5)."""
    _check_bounds(day)
    ts = _calendar(market).date_to_session(
        (day + timedelta(days=1)).isoformat(), direction="next")
    nxt: date = ts.date()
    return nxt


def session_open_utc(market: str, day: date) -> datetime:
    """UTC open timestamp of `day`'s session; raises if `day` is not a session."""
    _check_bounds(day)
    opened: datetime = _calendar(market).session_open(day.isoformat()).to_pydatetime()
    return opened


def session_close_utc(market: str, day: date) -> datetime:
    """UTC close timestamp of `day`'s session; raises if `day` is not a session.
    Early closes (e.g. XNYS half-days) come from the calendar, not from us."""
    _check_bounds(day)
    closed: datetime = _calendar(market).session_close(day.isoformat()).to_pydatetime()
    return closed


def last_completed_session(market: str, at: datetime) -> date:
    """Latest session whose UTC close is at or before aware datetime `at`.

    This is the newest session a nightly ingest may request or store: a session
    still in progress (or not yet opened) would yield a partial bar, and partial
    bars are look-ahead poison for anything replaying "as of" that day. Closes
    come from the exchange calendar in UTC, so DST shifts and exchanges east of
    UTC (XASX labels sessions ahead of the UTC date) are the calendar's problem,
    never naive date arithmetic here."""
    if at.tzinfo is None:
        raise ValueError("last_completed_session requires an aware datetime")
    # Start two calendar days past the UTC date — safely ahead of any exchange's
    # local "today" — then walk back to the first close at or before `at`.
    d = at.date() + timedelta(days=2)
    _check_bounds(d)
    if not is_trading_day(market, d):
        d = previous_trading_day(market, d)
    while session_close_utc(market, d) > at:
        d = previous_trading_day(market, d)
    return d


def local_date(market: str, at: datetime) -> date:
    """Calendar date at the exchange's home timezone for aware datetime `at`.
    XASX (UTC+10/+11) reaches a given date up to ~11h before UTC does; gates for
    non-trading days must never be stamped for a local date that hasn't arrived."""
    if at.tzinfo is None:
        raise ValueError("local_date requires an aware datetime")
    return at.astimezone(_calendar(market).tz).date()


def trading_days_between(market: str, start: date, end: date) -> list[date]:
    """All sessions in [start, end] inclusive, ascending. Empty if start > end."""
    if start > end:
        return []
    _check_bounds(start)
    _check_bounds(end)
    return [ts.date() for ts in
            _calendar(market).sessions_in_range(start.isoformat(), end.isoformat())]


def recent_sessions(market: str, day: date, lookback: int = 1) -> list[date]:
    """The `lookback` sessions strictly before `day`, plus `day` itself when it
    is a session; ascending. This is what a daily ingest should expect bars for."""
    out: list[date] = [day] if is_trading_day(market, day) else []
    d = day
    for _ in range(lookback):
        d = previous_trading_day(market, d)
        out.insert(0, d)
    return out
