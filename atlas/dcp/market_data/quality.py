"""Data-quality gates (Doc 01 par.2 principle enforcement, Doc 05 market.data_quality_gates).

A RED gate for a market blocks all downstream workflow for that market. Rules v1.2:
- missing trading day in the expected calendar -> gap -> RED
- any expected instrument missing bars on an expected day -> RED (one instrument's
  bars must never mask another's hole; pass expected_symbols to enable)
- any bar older than expected as-of date (stale feed) -> RED
- day-over-day close move beyond sanity bound without a matching corporate action -> AMBER
- NEW in v1.2 (deep-history backfill): a symbol is expected on day D only from its
  inception onward, where inception = the earliest bar_date STORED for that
  instrument (pass ``inceptions`` from :func:`inception_map` to enable). Rationale:
  an unlisted instrument is not a data gap; a listed one missing IS. A deep backfill
  to 2010 must not paint RED gates for days before NDIA.AU (~2019), INDA (2012) or
  AVGO (IPO 2009) existed on the feed — but from an instrument's first stored bar
  onward, any hole is exactly as RED as under v1.1.

v1.2 fail-closed edges (deliberate, tested):
- a symbol in expected_symbols but ABSENT from ``inceptions`` has no stored bars at
  all: it stays expected on EVERY day. That is the needs-backfill state (a new
  universe entry before its deliberate backfill) and it must red the gate honestly,
  never vanish from coverage.
- inception derives from stored bars, so it is self-referential: the first day a
  brand-new symbol ever stores a bar defines its inception, and days before that
  first bar are green for it by construction. A real vendor hole at the very start
  of a listed instrument's history is therefore indistinguishable from a late
  listing — accepted and documented; from the first stored bar onward the gate is
  as strict as ever.
- ``inceptions=None`` (default) disables the filter entirely: every expected symbol
  is expected on every day, byte-for-byte the v1.1 behaviour.

Design note: evaluate_gate keeps its signature stable and grows the optional
``inceptions`` mapping instead of taking per-day expected sets. All three callers
(backfill, daily, ingest_day) build ONE expected set per market; deriving the
per-day sets from the monotone inception rule belongs here, next to the other
versioned gate rules, not re-implemented in three call sites.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.market_data.models import Bar, GateStatus

RULES_VERSION = "1.2"
SANITY_MOVE = Decimal("0.40")  # 40% single-day move flags amber unless action explains it


@dataclass(frozen=True)
class GateResult:
    market: str
    gate_date: date
    status: GateStatus
    reasons: tuple[str, ...]


def inception_map(session: Session, market: str | None = None) -> dict[str, date]:
    """Earliest STORED bar_date per active instrument symbol (rules v1.2 input).

    Inception is derived from ``market.price_bars_daily`` — no schema change, no
    hand-maintained listing dates: the vendor's own first delivered bar defines
    when the series begins. Callers must invoke this AFTER upserting a run's bars
    (same transaction) so a deep backfill's own writes extend inceptions backward.
    Instruments with no stored bars at all are absent from the map — evaluate_gate
    treats absence as fail-closed (expected on every day, honestly RED)."""
    rows = session.execute(text(
        "SELECT i.symbol, min(pb.bar_date) AS inception "
        "FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.is_active AND (CAST(:m AS text) IS NULL OR i.market = :m) "
        "GROUP BY i.symbol"), {"m": market}).mappings()
    return {r["symbol"]: r["inception"] for r in rows}


def evaluate_gate(*, market: str, as_of: date, expected_days: list[date],
                  bars_by_day: dict[date, list[Bar]],
                  explained_symbols: frozenset[str] = frozenset(),
                  expected_symbols: frozenset[str] = frozenset(),
                  inceptions: Mapping[str, date] | None = None) -> GateResult:
    reasons: list[str] = []
    status = GateStatus.GREEN

    def expected_on(d: date) -> frozenset[str]:
        """Symbols expected on day d. Without inception info every expected
        symbol is expected always (v1.1; fail closed). With it, a symbol is
        expected from its first stored bar onward; a symbol with NO stored bars
        stays expected on every day (needs-backfill state, honestly RED)."""
        if inceptions is None:
            return expected_symbols
        return frozenset(s for s in expected_symbols
                         if s not in inceptions or inceptions[s] <= d)

    # A day is a gap when someone was expected and nothing arrived. Without
    # expected_symbols (day-level mode) any empty expected day is a gap, as in
    # v1.0/v1.1 — inception-awareness needs per-instrument coverage to be honest.
    if expected_symbols:
        missing = [d for d in expected_days
                   if expected_on(d) and not bars_by_day.get(d)]
    else:
        missing = [d for d in expected_days if not bars_by_day.get(d)]
    if missing:
        reasons.append(f"missing bars for {len(missing)} expected day(s): {missing[:3]}")
        status = GateStatus.RED

    if expected_symbols:
        for d in expected_days:
            got = {b.symbol for b in bars_by_day.get(d, [])}
            holes = sorted(expected_on(d) - got)
            if holes and got:  # fully-missing days are already RED above
                reasons.append(
                    f"{len(holes)} instrument(s) missing bars on {d}: {holes[:3]}")
                status = GateStatus.RED

    # expected_on is monotone in d, so an empty expected set on the LAST expected
    # day means nothing was listed anywhere in the window: staleness is vacuous
    # and the day is green — an unlisted instrument is not a data gap (v1.2).
    nothing_listed = (bool(expected_symbols) and inceptions is not None
                      and bool(expected_days) and not expected_on(max(expected_days)))
    if nothing_listed:
        reasons.append(f"rules v{RULES_VERSION}: no expected instrument incepted "
                       f"on or before {max(expected_days)}; an unlisted instrument "
                       "is not a data gap")
    elif expected_days and max(bars_by_day.keys(), default=date.min) < as_of:
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
