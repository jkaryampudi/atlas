"""Data-quality gates (Doc 01 par.2 principle enforcement, Doc 05 market.data_quality_gates).

A RED gate for a market blocks all downstream workflow for that market. Rules v1:
- missing trading day in the expected calendar -> gap -> RED
- any bar older than expected as-of date (stale feed) -> RED
- day-over-day close move beyond sanity bound without a matching corporate action -> AMBER
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from atlas.dcp.market_data.models import Bar, GateStatus

SANITY_MOVE = Decimal("0.40")  # 40% single-day move flags amber unless action explains it


@dataclass(frozen=True)
class GateResult:
    market: str
    gate_date: date
    status: GateStatus
    reasons: tuple[str, ...]


def evaluate_gate(*, market: str, as_of: date, expected_days: list[date],
                  bars_by_day: dict[date, list[Bar]],
                  explained_symbols: frozenset[str] = frozenset()) -> GateResult:
    reasons: list[str] = []
    status = GateStatus.GREEN

    missing = [d for d in expected_days if d not in bars_by_day or not bars_by_day[d]]
    if missing:
        reasons.append(f"missing bars for {len(missing)} expected day(s): {missing[:3]}")
        status = GateStatus.RED

    if expected_days and max(bars_by_day.keys(), default=date.min) < as_of:
        latest = max(bars_by_day.keys(), default=None)
        reasons.append(f"stale feed: latest bar {latest} < as_of {as_of}")
        status = GateStatus.RED

    if status is not GateStatus.RED:
        days = sorted(bars_by_day.keys())
        for prev_d, cur_d in zip(days, days[1:]):
            prev_close = {b.symbol: b.close for b in bars_by_day[prev_d]}
            for b in bars_by_day[cur_d]:
                pc = prev_close.get(b.symbol)
                if pc and pc > 0 and b.symbol not in explained_symbols:
                    move = abs(b.close - pc) / pc
                    if move > SANITY_MOVE:
                        reasons.append(f"{b.symbol} moved {move:.0%} {prev_d}->{cur_d} unexplained")
                        status = GateStatus.AMBER
    return GateResult(market=market, gate_date=as_of, status=status, reasons=tuple(reasons))
