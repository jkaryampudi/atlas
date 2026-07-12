"""Value objects for the market-data plane. Pure data, no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


class GateStatus(StrEnum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass(frozen=True)
class Bar:
    symbol: str
    bar_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"OHLC inconsistency for {self.symbol} {self.bar_date}")
        if self.volume < 0:
            raise ValueError("negative volume")


@dataclass(frozen=True)
class Split:
    symbol: str
    action_date: date  # effective (ex-) date
    ratio: Decimal     # 10-for-1 split -> Decimal(10)


@dataclass(frozen=True)
class Dividend:
    """Cash dividend, stored RAW (the declared per-share amount as of its
    ex-date) — never the vendor's split-adjusted figure, which is retroactively
    rewritten by every future split. Adjustment happens on read, exactly like
    bars (adjustment.adjust_for_splits / total_return.adjust_dividends_for_splits)."""
    symbol: str
    ex_date: date          # ex-dividend date (the vendor's `date` field)
    amount: Decimal        # declared cash per share, > 0
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError(f"non-positive dividend {self.amount} for "
                             f"{self.symbol} {self.ex_date}")
