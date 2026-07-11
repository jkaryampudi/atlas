"""Paper broker (Doc 08 Phase 5): deterministic next-session-open fills.

An order decided on day D fills at the exchange's NEXT session open after D,
priced from `market.price_bars_daily` (source EodhdAdapter — real vendor bars
only) with the backtester's CostModel commission+slippage bps applied, so paper
fills and backtest fills share one cost convention (Doc 04 §9 tolerance bands
stay comparable). No wall clock anywhere: the fill date comes from exchange
calendars, the price from stored bars, and `as_of` from the caller's injected
clock — replaying the same day yields byte-identical fills.

If the next session's bar does not exist yet (today's proposal, tomorrow's
open unknown), the session has not opened yet per the injected clock, or the
fill-date FX rate has not been ingested, `submit` returns None and the order
stays 'pending_submit' — the normal overnight state, never an error (Doc 05
§5 order states). The FX gate mirrors the bar gate: a fill is priced with the
fill date's OWN rate or not at all, so the immutable execution row never bakes
in a stale weekend rate.

Implementation shortfall (Doc 04 §14): every fill carries the decision price
and the realised shortfall in bps so the monthly attribution and cost-model
recalibration have per-trade inputs.

Note: fills need an exchange calendar; only US (XNYS) and AU (XASX) are mapped
(ADR-0002: India exposure trades via US-listed ETFs/ADRs, never NSE directly).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.market_data.calendars import next_trading_day, session_open_utc

PRICE_SOURCE = "EodhdAdapter"  # real vendor bars only, never fixtures
_PRICE_QUANT = Decimal("0.000001")
_BPS_QUANT = Decimal("0.0001")


@dataclass(frozen=True)
class OrderTicket:
    """Broker-facing view of one 'pending_submit' order (trading.orders)."""
    order_id: str
    instrument_id: UUID
    market: str                 # exchange-calendar key: 'US' | 'AU'
    currency: str               # instrument local currency
    side: Literal["buy", "sell"]
    qty: int
    decision_price: Decimal     # proposal entry price — Doc 04 §14 decision price
    decision_date: date         # UTC date the order was created (approval day)


@dataclass(frozen=True)
class Fill:
    fill_date: date
    fill_qty: int
    fill_price: Decimal         # effective price incl. commission+slippage bps
    fees: Decimal               # 0 — costs are embedded in the effective price
    fx_to_aud: Decimal          # AUD per 1 unit of local currency on fill date
    decision_price: Decimal
    shortfall_bps: Decimal      # signed: positive = worse than decision (§14)
    executed_at: datetime       # the fill session's open, UTC


class Broker(Protocol):
    """Doc 01 §9: the execution service speaks to any broker through this seam;
    Phase 7 live brokers implement the same protocol behind the arming gate.
    `as_of` is the injected clock's instant (not just a date) so a fill can
    never be recorded before its session opens."""

    def submit(self, session: Session, ticket: OrderTicket, *,
               as_of: datetime) -> Fill | None: ...


_OPEN_SQL = text(
    "SELECT open FROM market.price_bars_daily "
    "WHERE instrument_id = :iid AND bar_date = :d AND source = :src "
    "  AND open IS NOT NULL")

_FX_SQL = text(
    "SELECT rate FROM market.fx_rates_daily "
    "WHERE base = :base AND quote = 'AUD' AND rate_date <= :d "
    "ORDER BY rate_date DESC LIMIT 1")

_FX_EXACT_SQL = text(
    "SELECT rate FROM market.fx_rates_daily "
    "WHERE base = :base AND quote = 'AUD' AND rate_date = :d")


def fx_to_aud(session: Session, currency: str, on: date) -> Decimal:
    """Latest AUD translation rate on or before `on`, for MARKING the book.
    FAIL-CLOSED: a missing rate raises — nothing may be valued with unknown
    FX (Doc 03). Fills use fx_on_date instead: marks may carry the latest
    known rate, an immutable execution row may not."""
    if currency == "AUD":
        return Decimal(1)
    rate = session.execute(_FX_SQL, {"base": currency, "d": on}).scalar()
    if rate is None:
        raise RuntimeError(f"no {currency}->AUD rate on or before {on} — cannot price")
    return Decimal(rate)


def fx_on_date(session: Session, currency: str, on: date) -> Decimal | None:
    """The fill date's own AUD rate, or None while it is not ingested yet."""
    if currency == "AUD":
        return Decimal(1)
    rate = session.execute(_FX_EXACT_SQL, {"base": currency, "d": on}).scalar()
    return Decimal(rate) if rate is not None else None


def effective_price(px: Decimal, side: str, costs: CostModel) -> Decimal:
    """Raw price with commission+slippage bps applied, side-signed: buys pay
    up, sells receive less. Module-level (not a PaperBroker detail) so the
    stop-exit engine's intraday stop fills carry the SAME cost convention as
    next-open fills — one cost model per Doc 04 §9/§14."""
    bps = (Decimal(str(costs.commission_bps))
           + Decimal(str(costs.slippage_bps))) / Decimal(10_000)
    factor = (Decimal(1) + bps) if side == "buy" else (Decimal(1) - bps)
    return (px * factor).quantize(_PRICE_QUANT)


def shortfall_bps(fill_price: Decimal, decision_price: Decimal, side: str) -> Decimal:
    """Signed §14 implementation shortfall: positive = worse than the decision
    price on either side (buys filled higher, sells filled lower)."""
    sign = 1 if side == "buy" else -1
    return (sign * (fill_price - decision_price) * Decimal(10_000)
            / decision_price).quantize(_BPS_QUANT)


class PaperBroker:
    """Fills at the next session's open. The CostModel supplies the bps; the
    arithmetic is Decimal (CLAUDE.md invariant: Decimal for money — the
    backtester's float `buy`/`sell` never touch ledger amounts)."""

    def __init__(self, costs: CostModel | None = None) -> None:
        self._costs = costs if costs is not None else CostModel()

    def submit(self, session: Session, ticket: OrderTicket, *,
               as_of: datetime) -> Fill | None:
        """Resolve the fill deterministically, or None while the next session's
        bar, its open time, or its FX rate is still in the injected clock's
        future (the order remains 'pending_submit')."""
        fill_date = next_trading_day(ticket.market, ticket.decision_date)
        opens_at = session_open_utc(ticket.market, fill_date)
        if opens_at > as_of:
            return None  # session not opened yet — no clock look-ahead, even
            #              intraday over a backfilled DB (replay/live parity)
        open_px = session.execute(_OPEN_SQL, {
            "iid": ticket.instrument_id, "d": fill_date,
            "src": PRICE_SOURCE}).scalar()
        if open_px is None:
            return None  # bar not ingested yet — normal overnight state
        fx = fx_on_date(session, ticket.currency, fill_date)
        if fx is None:
            return None  # fill-date FX not ingested yet — same gate as the bar
        price = effective_price(Decimal(open_px), ticket.side, self._costs)
        shortfall = shortfall_bps(price, ticket.decision_price, ticket.side)
        return Fill(
            fill_date=fill_date,
            fill_qty=ticket.qty,
            fill_price=price,
            fees=Decimal(0),
            fx_to_aud=fx,
            decision_price=ticket.decision_price,
            shortfall_bps=shortfall,
            executed_at=opens_at,
        )
