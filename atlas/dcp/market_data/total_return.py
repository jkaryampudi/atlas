"""Total-return series construction (board memo 2026-07, item 1).

ADR-0009's binding benchmark is SPY TOTAL RETURN. Stored bars are RAW prices
(split-adjusted on read); stored dividends are RAW declared cash per share
(market.corporate_actions, action_type='dividend'). This module turns a
split-adjusted price series plus its split-adjusted ex-date dividends into a
TOTAL-RETURN series that any panel-based engine can consume unchanged.

THE CONVENTION (stated once, applied identically to strategy holdings, the
monkey null, the equal-weight benchmark and SPY, because all read one panel):
each cash dividend is REINVESTED AT THE EX-DATE'S CLOSE. Both the open and the
close of every session i are multiplied by a cumulative factor

    F(i) = prod over ex-dates e <= i of (1 + D_e / C_e)

where D_e is the split-adjusted dividend and C_e the split-adjusted PRICE
close on the ex-date. Because open(i) and close(i) share one factor, intraday
(open->close) relative moves are untouched on every session; the overnight
close(e-1)->open(e) leg — exactly where the price gaps down by the detached
dividend — absorbs the compensation. Consequences, all economically correct:
a holder across the ex-boundary is credited the dividend (including a seller
at the ex-date open, who is owed it); a buyer at the ex-date open gets no
credit (F is constant until the next ex-date). Reinvestment at the ex-date
close (the CRSP convention) ignores the ex->payment lag; S&P's payment-date
reinvestment differs immaterially at daily resolution and we state ours.

Honesty accounting: a dividend whose ex-date precedes the series' first bar
cannot be reinvested (no position could have held it) — DROPPED and counted;
an ex-date after the final bar belongs to a position already liquidated at its
final close under the delisting rule — DROPPED and counted; an ex-date inside
the series with no bar that day (off-calendar vendor quirk) ROLLS FORWARD to
the next session with a bar, counted. Nothing is silently discarded.

Split interaction: dividends adjust for splits exactly as prices do — a
dividend declared strictly BEFORE a split's effective date is divided by the
ratio (adjust_dividends_for_splits mirrors adjustment.adjust_for_splits), so
D_e / C_e is invariant to the adjustment basis by construction.

Pure math on floats (panel space) + one read-side loader. The frozen engines
(portfolio.py and the xsmom_pit delisting-aware sibling) are untouched: total
return enters as a LOADER-LEVEL transform of the panel they are handed.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.market_data.models import Dividend, Split


def adjust_dividends_for_splits(dividends: list[Dividend],
                                splits: list[Split]) -> list[Dividend]:
    """Split-adjust dividend amounts with the SAME rule as bars: an ex-date
    strictly BEFORE a split's effective date is divided by the ratio; multiple
    splits compound (adjustment.adjust_for_splits, applied to cash amounts).
    D/C stays invariant because numerator and denominator share the factor."""
    if not splits:
        return list(dividends)
    relevant = sorted(splits, key=lambda s: s.action_date)
    out: list[Dividend] = []
    for dv in dividends:
        factor = Decimal(1)
        for s in relevant:
            if s.symbol == dv.symbol and dv.ex_date < s.action_date:
                factor *= s.ratio
        if factor == 1:
            out.append(dv)
            continue
        out.append(Dividend(symbol=dv.symbol, ex_date=dv.ex_date,
                            amount=dv.amount / factor, currency=dv.currency))
    return out


def load_adjusted_dividends(session: Session, symbol: str) -> list[Dividend]:
    """Stored dividends for `symbol`, split-adjusted on read — the dividend
    sibling of real_run.load_adjusted_obars (raw in the DB, adjusted here)."""
    divs = [Dividend(symbol=symbol, ex_date=r.action_date,
                     amount=Decimal(r.amount), currency=r.currency)
            for r in session.execute(text(
                "SELECT ca.action_date, ca.amount, ca.currency "
                "FROM market.corporate_actions ca "
                "JOIN market.instruments i ON i.id = ca.instrument_id "
                "WHERE i.symbol = :s AND ca.action_type = 'dividend' "
                "ORDER BY ca.action_date"), {"s": symbol})]
    splits = [Split(symbol=symbol, action_date=r.action_date,
                    ratio=Decimal(r.ratio))
              for r in session.execute(text(
                  "SELECT ca.action_date, ca.ratio FROM market.corporate_actions ca "
                  "JOIN market.instruments i ON i.id = ca.instrument_id "
                  "WHERE i.symbol = :s AND ca.action_type = 'split'"),
                  {"s": symbol})]
    return adjust_dividends_for_splits(divs, splits)


@dataclass(frozen=True)
class TotalReturnSeries:
    """TR-transformed open/close series plus the honesty counts."""
    opens: list[float]
    closes: list[float]
    applied: int           # dividends folded into the factor
    dropped_before: int    # ex-date precedes the first bar (unreinvestable)
    dropped_after: int     # ex-date after the final bar (position already cash)
    rolled: int            # ex-date had no bar; reinvested at next session


def total_return_series(*, dates: list[date], opens: list[float],
                        closes: list[float],
                        dividends: list[Dividend]) -> TotalReturnSeries:
    """Apply the module-docstring convention: cumulative reinvestment factor
    stepping by (1 + D/C) at each ex-date's close, multiplying opens and
    closes alike. `dates` are the symbol's OWN bar dates (contiguous by the
    loader's completeness rule); dividends must be in the SAME adjustment
    basis as the prices (use load_adjusted_dividends with split-adjusted
    bars)."""
    n = len(dates)
    if not (n == len(opens) == len(closes)):
        raise ValueError("dates/opens/closes length mismatch")
    per_index: dict[int, float] = {}
    applied = dropped_before = dropped_after = rolled = 0
    for dv in dividends:
        if dv.ex_date < dates[0]:
            dropped_before += 1
            continue
        i = bisect_left(dates, dv.ex_date)
        if i >= n:
            dropped_after += 1
            continue
        if dates[i] != dv.ex_date:
            rolled += 1
        per_index[i] = per_index.get(i, 0.0) + float(dv.amount)
        applied += 1
    factor = 1.0
    opens_tr: list[float] = []
    closes_tr: list[float] = []
    for i in range(n):
        amt = per_index.get(i)
        if amt is not None:
            factor *= 1.0 + amt / closes[i]
        opens_tr.append(opens[i] * factor)
        closes_tr.append(closes[i] * factor)
    return TotalReturnSeries(opens=opens_tr, closes=closes_tr, applied=applied,
                             dropped_before=dropped_before,
                             dropped_after=dropped_after, rolled=rolled)
