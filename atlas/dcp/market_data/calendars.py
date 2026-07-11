"""Exchange trading calendars (Phase 1 checklist item).

Wraps `exchange_calendars` so the rest of the plane speaks plain `date`s:
US -> XNYS, AU -> XASX. Replaces the naive weekend-skip — holidays are
non-trading days, and gates must never demand bars for them.
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Any

import exchange_calendars as xcals

MARKET_CALENDARS = {"US": "XNYS", "AU": "XASX"}


@lru_cache(maxsize=None)
def _calendar(market: str) -> Any:
    try:
        code = MARKET_CALENDARS[market]
    except KeyError:
        raise ValueError(f"no exchange calendar mapped for market {market!r}") from None
    return xcals.get_calendar(code)


def is_trading_day(market: str, day: date) -> bool:
    return bool(_calendar(market).is_session(day.isoformat()))


def previous_trading_day(market: str, day: date) -> date:
    """Latest session strictly before `day` (skips weekends AND holidays)."""
    ts = _calendar(market).date_to_session(
        (day - timedelta(days=1)).isoformat(), direction="previous")
    prev: date = ts.date()
    return prev


def trading_days_between(market: str, start: date, end: date) -> list[date]:
    """All sessions in [start, end] inclusive, ascending. Empty if start > end."""
    if start > end:
        return []
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
