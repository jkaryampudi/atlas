"""Authoritative benchmark & reporting-basis return service (F-006).

ONE place computes a price series on the fund's reporting basis — **AUD, total
return, split-adjusted, FX-versioned** — so an excess is never again formed by
subtracting a USD price return from an INR (or AUD-sleeve) price return.

Before this module the benchmark leg was computed three incompatible ways:
  * demotion bands (bands.py) already did AUD total-return correctly;
  * the memo scorecard subtracted a raw-USD SPY price return from the
    instrument's OWN-currency price return (F-006 — the currency-inconsistent
    defect the review named);
so the fix is to give every consumer the SAME function.

THE BASIS (stated once, applied identically to the benchmark and to any graded
instrument, because a fair excess needs both legs on one basis):

  reporting_close(d) = total_return_close(d) x fx_to_aud(currency, d)

  * total_return_close: split-adjusted price close with dividends reinvested at
    the ex-date close (market_data/total_return.py — the frozen convention);
  * fx_to_aud: the PIT AUD translation rate at date d (execution/paper.py),
    fail-closed — a session whose return cannot be stated in AUD is not stated,
    so the whole series raises rather than emit a half-AUD number.

Because open(i) and close(i) share the TR factor and the SAME per-date FX is
applied to both legs of a same-currency comparison, pure FX drift cancels in the
difference: a flat-in-USD instrument vs a flat-in-USD SPY reads zero excess, not
the USD/AUD move. A genuinely non-USD instrument (India sleeve) is now converted
on its OWN currency's FX, so its AUD return is honest instead of pretending to
be USD.

Two-plane: pure DCP; injectable nothing beyond the Session (FX + bars are read
from stored tables). Decimal end to end for the reporting values; the TR
transform runs in float panel space exactly as total_return.py defines it.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.execution.paper import PRICE_SOURCE, fx_to_aud
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.models import Bar, Split
from atlas.dcp.market_data.total_return import (load_adjusted_dividends,
                                                total_return_series)

BENCHMARK_SYMBOL = "SPY"          # ADR-0009: the fund's honest alternative
BENCHMARK_CURRENCY = "USD"        # SPY is USD-denominated
VENDOR_SOURCE = PRICE_SOURCE      # 'EodhdAdapter' — the same vendor-bar discipline


def _split_adjusted_closes(session: Session, instrument_id: str,
                           through: date) -> list[tuple[date, Decimal]]:
    """Ascending vendor closes for one instrument, hard-capped at `through` (no
    look-ahead) and split-adjusted on read using only splits with action_date
    <= through. Degenerate OHLC exists only to satisfy the Bar invariant; only
    close is read back."""
    rows = session.execute(text(
        "SELECT bar_date, close FROM market.price_bars_daily "
        "WHERE instrument_id = :iid AND source = :src "
        "  AND close IS NOT NULL AND bar_date <= :d ORDER BY bar_date"),
        {"iid": instrument_id, "src": VENDOR_SOURCE, "d": through}).all()
    if not rows:
        return []
    splits = [Split(symbol=instrument_id, action_date=r.action_date,
                    ratio=Decimal(r.ratio))
              for r in session.execute(text(
                  "SELECT action_date, ratio FROM market.corporate_actions "
                  "WHERE instrument_id = :iid AND action_type = 'split' "
                  "  AND action_date <= :d ORDER BY action_date"),
                  {"iid": instrument_id, "d": through}).all()]
    if not splits:
        return [(r.bar_date, Decimal(r.close)) for r in rows]
    bars = [Bar(symbol=instrument_id, bar_date=r.bar_date, open=Decimal(r.close),
                high=Decimal(r.close), low=Decimal(r.close), close=Decimal(r.close),
                volume=0) for r in rows]
    return [(b.bar_date, b.close) for b in adjust_for_splits(bars, splits)]


def reporting_close_series(
        session: Session, *, instrument_id: str, symbol: str, currency: str,
        through: date, known_by: datetime | None = None) -> dict[date, Decimal]:
    """The authoritative reporting-basis series: ``date -> total-return close in
    AUD`` for every stored vendor bar of this instrument dated <= ``through``.

    Split-adjusted prices, dividends reinvested at the ex-date close (total
    return), each session's close multiplied by that session's ``fx_to_aud``.
    FAIL-CLOSED on FX: if any date in the series has no resolvable AUD rate,
    ``fx_to_aud`` raises and the whole load raises — the caller then treats the
    instrument as unpriceable-in-AUD and fails closed (no half-AUD series ever
    reaches a return)."""
    prices = _split_adjusted_closes(session, instrument_id, through)
    if not prices:
        return {}
    dates = [d for d, _ in prices]
    closes = [float(c) for _, c in prices]
    divs = [d for d in load_adjusted_dividends(session, symbol, known_by=known_by)
            if d.ex_date <= through]
    trs = total_return_series(dates=dates, opens=list(closes), closes=closes,
                              dividends=divs)
    out: dict[date, Decimal] = {}
    for d, tr_close in zip(dates, trs.closes, strict=True):
        out[d] = Decimal(str(tr_close)) * fx_to_aud(session, currency, d)
    return out


def _resolve_benchmark_instrument(session: Session) -> str | None:
    """The one ACTIVE SPY instrument (the bridge/scorecard rule); with none
    active, exactly one row of any activity resolves. Ambiguity fails closed."""
    rows = session.execute(text(
        "SELECT id, is_active FROM market.instruments WHERE symbol = :s"),
        {"s": BENCHMARK_SYMBOL}).all()
    active = [r for r in rows if r.is_active]
    if len(active) == 1:
        return str(active[0].id)
    if not active and len(rows) == 1:
        return str(rows[0].id)
    return None


def benchmark_reporting_series(
        session: Session, *, through: date,
        known_by: datetime | None = None) -> dict[date, Decimal]:
    """SPY on the reporting basis: ``date -> SPY total-return close in AUD`` for
    every stored SPY bar dated <= ``through``. Empty when SPY does not resolve to
    a single instrument — the caller then fails closed exactly as it does for a
    missing benchmark today."""
    iid = _resolve_benchmark_instrument(session)
    if iid is None:
        return {}
    return reporting_close_series(session, instrument_id=iid,
                                  symbol=BENCHMARK_SYMBOL,
                                  currency=BENCHMARK_CURRENCY,
                                  through=through, known_by=known_by)


def reporting_return_between(session: Session, *, symbol: str, on: date,
                             prev: date) -> Decimal | None:
    """AUD total-return close-to-close return of ``symbol`` between the two EXACT
    session dates ``prev`` -> ``on`` (the daily-attribution consumer's view).
    Both legs on the AUD reporting basis via PIT ``fx_to_aud`` on each date, so a
    sleeve marked in AUD is graded against a benchmark in AUD and pure FX drift
    cancels in the excess (F-006). None unless BOTH exact dates have vendor bars;
    missing FX fails closed (fx_to_aud raises), exactly like the marking path.

    Reads by symbol (the caller's contract); the currency comes from the bar's
    own instrument row. This is the single home of the reporting-basis return —
    the demotion bands, the memo scorecard, and daily attribution all grade
    against numbers produced here or by ``reporting_close_series`` above."""
    rows = session.execute(text(
        "SELECT pb.bar_date, pb.close, i.currency FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol = :sym AND pb.source = :src AND pb.bar_date <= :on "
        "  AND pb.close IS NOT NULL ORDER BY pb.bar_date"),
        {"sym": symbol, "src": VENDOR_SOURCE, "on": on}).all()
    dates = [r.bar_date for r in rows]
    if not dates or dates[-1] != on or prev not in dates:
        return None
    cur = str(rows[0].currency)
    closes = [float(r.close) for r in rows]
    trs = total_return_series(
        dates=dates, opens=list(closes), closes=closes,
        dividends=[d for d in load_adjusted_dividends(session, symbol)
                   if d.ex_date <= on])
    tr_on = Decimal(str(trs.closes[-1])) * fx_to_aud(session, cur, on)
    tr_prev = Decimal(str(trs.closes[dates.index(prev)])) * fx_to_aud(session, cur, prev)
    if tr_prev <= 0:
        return None
    return tr_on / tr_prev - 1


def benchmark_reporting_close(session: Session, on: date, *,
                              known_by: datetime | None = None) -> Decimal | None:
    """SPY total-return close in AUD as of the last SPY bar at or before ``on``
    (the demotion-band consumer's single-point view). None when SPY has no bar
    at/before ``on`` — the excess stays dormant; DD still enforces."""
    series = benchmark_reporting_series(session, through=on, known_by=known_by)
    if not series:
        return None
    return series[max(series)]
