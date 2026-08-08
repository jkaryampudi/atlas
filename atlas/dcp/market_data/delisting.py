"""Delisting watch: the SATS/EA pattern, surfaced instead of suffered.

Twice in one week an S&P member was vendor-delisted (SATS 2026-06-23, EA
2026-08-05) while staying `is_active=true` locally — so the nightly US quality
gate went RED ("1 instrument missing bars") and every cycle was marked failed
until an operator diagnosed it by hand. This module turns that mystery into an
explicit surface:

  * ``find_delisting_candidates`` — read-only: every ACTIVE US stock whose
    stored bars have stopped before the last completed session, probed against
    the vendor's fundamentals (IsDelisted/DelistedDate, fail-soft per symbol).
    Zero candidates costs zero vendor calls.
  * ``deactivate_delisted`` — the guarded, audited one-click: re-verifies the
    delisting AT THE VENDOR server-side (never trusts the console click),
    REFUSES a held name (capital preservation first), then flips
    ``is_active=false``, closes the index-membership row at the vendor's
    delisted date, and appends one audit event. Same shape as the operator
    fixes for SATS/EA, now repeatable without raw SQL.

Deactivation never deletes anything: bars, memos and history stay; the gate
simply stops expecting tomorrow's bar from a stock that no longer trades.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.calendars import last_completed_session

AdapterFor = Callable[[str, str], MarketDataAdapter]

INDEX_CODE = "GSPC.INDX"


class DelistingError(RuntimeError):
    """A refused deactivation — the reason is the message (held, not delisted
    at the vendor, unknown symbol). Fail closed, never guess."""


@dataclass(frozen=True)
class DelistingCandidate:
    symbol: str
    last_bar: date | None
    vendor_delisted: bool | None       # None = vendor probe failed (fail-soft)
    delisted_date: str | None
    held: bool


@dataclass(frozen=True)
class DeactivationResult:
    symbol: str
    delisted_date: date
    membership_rows: int
    audit_seq: int


def _held_qty(session: Session, symbol: str) -> int:
    return int(session.execute(text(
        "SELECT COALESCE(sum(abs(p.qty)), 0) FROM trading.positions p "
        "JOIN market.instruments i ON i.id = p.instrument_id "
        "WHERE i.symbol = :s"), {"s": symbol}).scalar() or 0)


def _vendor_delisting(adapter_for: AdapterFor, symbol: str,
                      exchange: str) -> tuple[bool | None, str | None]:
    """(is_delisted, delisted_date) from the vendor's fundamentals; (None, None)
    when the probe fails — reported honestly, never guessed."""
    try:
        payload = adapter_for(symbol, exchange).fetch_fundamentals(symbol)
        general = (payload or {}).get("General")
        if not isinstance(general, dict):
            return None, None
        ddate = general.get("DelistedDate")
        return bool(general.get("IsDelisted")), (str(ddate) if ddate else None)
    except Exception:  # noqa: BLE001 — fail-soft probe, surfaced as unknown
        return None, None


def find_delisting_candidates(session: Session, clock: Clock,
                              adapter_for: AdapterFor) -> list[DelistingCandidate]:
    """Active US stocks whose stored bars stop before the last completed US
    session. Bounded: the vendor is probed only for these (normally zero)."""
    last_session = last_completed_session("US", clock.now())
    rows = session.execute(text(
        "SELECT i.symbol, i.exchange, "
        "       (SELECT max(pb.bar_date) FROM market.price_bars_daily pb "
        "         WHERE pb.instrument_id = i.id) AS last_bar "
        "FROM market.instruments i "
        "WHERE i.market = 'US' AND i.is_active AND i.instrument_type = 'stock'")).all()
    out: list[DelistingCandidate] = []
    for r in rows:
        if r.last_bar is not None and r.last_bar >= last_session:
            continue
        delisted, ddate = _vendor_delisting(adapter_for, r.symbol, r.exchange)
        out.append(DelistingCandidate(
            symbol=str(r.symbol), last_bar=r.last_bar, vendor_delisted=delisted,
            delisted_date=ddate, held=_held_qty(session, r.symbol) > 0))
    out.sort(key=lambda c: c.symbol)
    return out


def deactivate_delisted(session: Session, clock: Clock, adapter_for: AdapterFor,
                        symbol: str) -> DeactivationResult:
    """The guarded one-click. Every guard fails CLOSED with the reason:
    unknown/inactive symbol, a held position, or a vendor that does not confirm
    the delisting (with a date). The vendor is re-probed here — the caller's
    belief is never trusted."""
    row = session.execute(text(
        "SELECT id, exchange, is_active FROM market.instruments "
        "WHERE symbol = :s AND market = 'US'"), {"s": symbol}).first()
    if row is None:
        raise DelistingError(f"unknown US instrument {symbol!r}")
    if not row.is_active:
        raise DelistingError(f"{symbol} is already inactive — nothing to do")
    held = _held_qty(session, symbol)
    if held:
        raise DelistingError(
            f"{symbol} is HELD (qty {held}) — refusing to deactivate a held "
            "name; exit the position through the normal paths first")
    delisted, ddate = _vendor_delisting(adapter_for, symbol, str(row.exchange))
    if delisted is not True or not ddate:
        raise DelistingError(
            f"vendor does not confirm {symbol} as delisted "
            f"(IsDelisted={delisted!r}, DelistedDate={ddate!r}) — refusing")
    delisted_on = date.fromisoformat(ddate)

    session.execute(text(
        "UPDATE market.instruments SET is_active = FALSE WHERE id = :i"),
        {"i": row.id})
    membership_result = session.execute(text(
        "UPDATE validation.index_membership "
        "SET is_delisted = TRUE, is_active_now = FALSE, end_date = :d "
        "WHERE ticker = :s AND index_code = :ic"),
        {"s": symbol, "d": delisted_on, "ic": INDEX_CODE})
    membership_rows = int(getattr(membership_result, "rowcount", 0) or 0)
    ev = PostgresAuditLog(session, clock).append(
        event_type="market.universe.deactivated", entity_type="market",
        entity_id="universe", actor_type="human", actor_id="console-operator",
        payload={"deactivated": [symbol], "instrument_id": str(row.id),
                 "delisted_date": ddate,
                 "reason": ("vendor-delisted — one-click console deactivation "
                            "(delisting watch); vendor re-verified server-side; "
                            "not held")})
    return DeactivationResult(symbol=symbol, delisted_date=delisted_on,
                              membership_rows=membership_rows,
                              audit_seq=int(ev.seq))
