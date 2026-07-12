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


# The ONLY vendor timing flags admitted into market.earnings_calendar.when_time
# (closed vocabulary — anything else the vendor sends is normalized to None at
# the adapter boundary and never stored, let alone rendered).
EARNINGS_WHEN_TIMES = frozenset({"BeforeMarket", "AfterMarket"})


@dataclass(frozen=True)
class EarningsEvent:
    """One vendor earnings-calendar entry: the report date (announcement day)
    plus the optional closed-vocabulary before/after-market flag. Deliberately
    NO actual/estimate/surprise fields: v1 stores dates only (desk-review memo
    2026-07 item 9 — the evidence block is ISO dates and session counts, zero
    vendor prose)."""
    symbol: str
    report_date: date
    when_time: str | None = None

    def __post_init__(self) -> None:
        if self.when_time is not None and self.when_time not in EARNINGS_WHEN_TIMES:
            raise ValueError(f"when_time {self.when_time!r} not in the closed "
                             f"vocabulary {sorted(EARNINGS_WHEN_TIMES)}")


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
