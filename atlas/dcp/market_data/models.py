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
