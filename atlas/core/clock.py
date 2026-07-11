"""Injectable time. No module in atlas/ may call datetime.now() directly.

Backtests, deterministic replays, and audit verification all depend on controllable time.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FrozenClock:
    """Deterministic clock for tests and replays."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance_to(self, at: datetime) -> None:
        if at < self._at:
            raise ValueError("clock cannot move backwards")
        self._at = at
