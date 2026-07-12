"""Exit engine (Doc 04 §5 exit-only breakers, §14 shortfall; Doc 05 §5 states).

Two ways out of a position, both pure deterministic compute plane (no agent
import, injectable Clock only, every material action audited):

  scan_stop_exits  — the protective stop the human approved WITH the entry
                     (Doc 04: the stop is part of the approved proposal) fires
                     against ingested daily bars. Pre-authorized: it creates
                     the sell order directly — no new proposal, no fresh human
                     click — and fills it in the same transaction.
  close_position   — the human wants out ahead of the stop. Builds an EXIT
                     proposal that flows through the normal approve() ->
                     sell order -> next-open settle path.

Documented resolutions (Doc ambiguities, resolved conservatively):
- Pre-authorization lineage: orders.approval_id / orders.risk_check_id are
  NOT NULL, so a stop-exit order references the ORIGINAL entry approval (the
  human click that authorized the stop) plus a FRESH PASS risk check of kind
  'order_time' whose results record WHY the order exists (rule 'STOP', the
  stop, the low, the date). limit_set_version on exit-path checks stays NULL:
  no limit set is consulted — exits release risk, L1-L11 gate risk being
  ADDED (close_position's 'proposal' and approve()'s 'approval_time' exit
  checks follow the same convention).
- Entry lineage is resolved through the position's OLDEST tax lot ->
  execution -> order -> (proposal, approval). Positions without a lot trail
  cannot resolve an approval and fail closed (raise) — the lifecycle always
  writes lots, so only hand-seeded rows can hit this.
- Trigger bar: the LATEST ingested bar with bar_date <= the clock's UTC date
  and bar_date strictly AFTER the position's opened_at UTC date. The entry
  day's own bar can never re-trigger (its low may predate the fill), and
  older intermediate bars are not re-scanned — the daily scan cadence owns
  catching each bar as it lands; a dip that healed before the scan ran is a
  missed stop, recorded nowhere because it triggered nothing.
- Idempotency: a position with a LIVE sell order (pending_submit, submitted
  or partially_filled) created at/after its opened_at is skipped — standing
  exit intent means a second sell of the same shares must be unrepresentable.
  Dead orders (cancelled/rejected/error) do NOT block: a cancelled exit is
  WITHDRAWN intent, and the protective stop must come back to life — anything
  else silently disarms the stop forever after one cancel. Filled sells don't
  need to block: a full fill closes the position out of the scan, and after a
  partial fill the stop SHOULD still protect the remainder. (A pending exit
  PROPOSAL without an order does not block either: stop protection stays live
  until the human actually approves; approve()'s EXIT re-check then voids the
  stale proposal.)
- Fill price = min(stop, bar open): a gap-down open fills at the open (you
  cannot fill above where the market opened), a normal intraday hit fills at
  the stop — with SELL-side CostModel bps applied via the shared
  effective_price, so stop fills and next-open fills carry one cost
  convention (Doc 04 §9/§14).
- decision_price = the stop itself (the price the human pre-authorized), so
  shortfall_bps isolates gap cost + friction against the authorized level.
- executed_at = the bar day's session open (the same timestamp convention as
  the PaperBroker; the daily bar does not say WHEN intraday the low printed).
  A clock still before that open skips the position this run — no fill may
  be recorded before its session opens (replay/live parity).
- FX: the bar date's OWN rate (fx_on_date) or skip this position this run —
  fail closed, retry next scan; a stale rate must never enter the immutable
  execution row.
- NOTHING in the stop path marks the book: no _latest_close, no fx_to_aud
  marking calls — a stale close or missing FX on an UNRELATED holding must
  never block a protective exit. The breaker recorded in the check row is
  folded from the persisted NAV history alone.
- DD3 is exit-only, never exit-blocking (Doc 04 §5): the scan consults no
  breaker gate and no engine.validate; the latched level is recorded in the
  check's price_snapshot as evidence that exits ran under it.
- close_position resolutions: committee_memo_id = the position's
  thesis_memo_id (the memo that justified holding the position justifies
  unwinding it — no new committee evidence is required to REDUCE risk);
  signal_ids are copied from the entry proposal (NOT NULL, cardinality > 0:
  the signals that opened the trade are the ones being closed out);
  entry_price = target_price = the latest vendor close (fail-closed mark of
  the exited instrument itself — if IT cannot be marked the exit cannot be
  priced); stop_loss = the current stop, or the latest close when the
  position has none (the column is NOT NULL; the value is descriptive for an
  exit, never used for sizing).
- close_position refuses while an exit is already in flight (a live exit
  proposal in 'pending_approval'/'approved' or an unfilled sell order for
  the instrument): approving both would sell the same shares twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.execution.paper import (
    PRICE_SOURCE,
    Fill,
    effective_price,
    fx_on_date,
    fx_to_aud,
    shortfall_bps,
)
from atlas.dcp.market_data.calendars import session_open_utc
from atlas.dcp.trading.proposals import (
    PROPOSAL_TTL,
    ProposalResult,
    _audit,
    _breaker_fold,
    _CENT,
    _confirmed_clearances,
    _latest_close,
    _lifecycle_lock,
    _persist_static_check,
    _record_fill,
    _snapshot_navs,
)


@dataclass(frozen=True)
class StopExitReport:
    position_id: str
    order_id: str
    execution_id: str
    symbol: str
    fill_date: date
    fill_price: Decimal          # effective sell price incl. bps
    qty: int
    shortfall_bps: Decimal       # vs the authorized stop (gap cost + friction)


@dataclass(frozen=True)
class _EntryLineage:
    proposal_id: UUID
    approval_id: UUID
    signal_ids: list[UUID]


def _entry_lineage(session: Session, position_id: UUID) -> _EntryLineage:
    """The entry proposal/approval behind a position, via its OLDEST tax lot
    (module docstring). Fail closed: no lot trail, no pre-authorization."""
    row = session.execute(text(
        "SELECT o.proposal_id, o.approval_id, tp.signal_ids "
        "FROM trading.tax_lots tl "
        "JOIN trading.executions e ON e.id = tl.execution_id "
        "JOIN trading.orders o ON o.id = e.order_id "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "WHERE tl.position_id = :p "
        "ORDER BY tl.acquired_at, tl.created_at, tl.id LIMIT 1"),
        {"p": position_id}).first()
    if row is None:
        raise RuntimeError(f"position {position_id} has no tax-lot lineage to an "
                           "entry approval — cannot pre-authorize an exit order")
    return _EntryLineage(proposal_id=row.proposal_id, approval_id=row.approval_id,
                         signal_ids=list(row.signal_ids))


def _order_row(session: Session, order_id: UUID) -> Any:
    """The order in settle_orders' row shape, so _record_fill is shared
    verbatim between next-open settlement and same-transaction stop fills."""
    return session.execute(text(
        "SELECT o.id, o.qty, o.side, o.created_at, tp.id AS proposal_id, "
        "       tp.state AS proposal_state, tp.entry_price, tp.stop_loss, "
        "       tp.committee_memo_id, i.id AS iid, i.symbol, i.market, i.currency "
        "FROM trading.orders o "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE o.id = :o"), {"o": order_id}).one()


# ------------------------------------------------------------- stop exits (§5)

def scan_stop_exits(session: Session, clock: Clock,
                    costs: CostModel | None = None) -> tuple[StopExitReport, ...]:
    """Fire every protective stop the latest ingested bars have hit.

    For each open position with a current_stop: take the latest bar strictly
    after the entry date; if its low touched the stop, create the
    pre-authorized sell order (original entry approval + fresh PASS
    'order_time' check) and fill it in the same transaction at
    min(stop, open) with sell-side costs. Skips are silent and re-scannable:
    no bar yet, stop not hit, session not open per the injected clock, or
    fill-date FX missing (fail closed, retry next run). Runs under DD3 by
    design — the breaker is exit-only, never exit-blocking (Doc 04 §5)."""
    _lifecycle_lock(session)
    c = costs if costs is not None else CostModel()
    now = clock.now()
    today = now.date()
    audit = _audit(session, clock)
    # breaker from persisted NAV history only — the stop path never marks the
    # book (module docstring: an unrelated stale close must not block an exit)
    breaker = _breaker_fold(_snapshot_navs(session), _confirmed_clearances(session))
    positions = session.execute(text(
        "SELECT p.id, p.qty, p.current_stop, p.opened_at, i.id AS iid, i.symbol, "
        "       i.market, i.currency "
        "FROM trading.positions p JOIN market.instruments i ON i.id = p.instrument_id "
        "WHERE p.closed_at IS NULL AND p.qty > 0 AND p.current_stop IS NOT NULL "
        "  AND p.opened_at IS NOT NULL ORDER BY p.opened_at FOR UPDATE OF p")).all()

    reports: list[StopExitReport] = []
    for p in positions:
        # idempotency: LIVE sell intent blocks a new exit; dead orders
        # (cancelled/rejected/error) must NOT — a withdrawn exit re-arms the
        # stop, and a filled one either closed the position or left a
        # remainder the stop should still protect (module docstring)
        exit_exists = session.execute(text(
            "SELECT 1 FROM trading.orders o "
            "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
            "WHERE o.side = 'sell' AND tp.instrument_id = :iid "
            "  AND o.state IN ('pending_submit','submitted','partially_filled') "
            "  AND o.created_at >= :opened LIMIT 1"),
            {"iid": p.iid, "opened": p.opened_at}).first()
        if exit_exists is not None:
            continue
        opened_date = p.opened_at.astimezone(UTC).date()
        bar = session.execute(text(
            "SELECT bar_date, open, low FROM market.price_bars_daily "
            "WHERE instrument_id = :iid AND source = :src "
            "  AND bar_date <= :today AND bar_date > :opened "
            "  AND open IS NOT NULL AND low IS NOT NULL "
            "ORDER BY bar_date DESC LIMIT 1"),
            {"iid": p.iid, "src": PRICE_SOURCE, "today": today,
             "opened": opened_date}).first()
        if bar is None:
            continue                    # no post-entry bar yet
        stop = Decimal(p.current_stop)
        low = Decimal(bar.low)
        if low > stop:
            continue                    # stop not hit on the latest bar
        opens_at = session_open_utc(p.market, bar.bar_date)
        if opens_at > now:
            continue                    # session not open per the injected clock
        fx = fx_on_date(session, p.currency, bar.bar_date)
        if fx is None:
            continue                    # fail closed: no fill-date FX, retry
        raw = min(stop, Decimal(bar.open))   # gap-down opens fill at the open
        price = effective_price(raw, "sell", c)
        sf = shortfall_bps(price, stop, "sell")
        qty = int(p.qty)

        lineage = _entry_lineage(session, p.id)
        check_id = _persist_static_check(
            session, clock, proposal_id=lineage.proposal_id, kind="order_time",
            verdict="PASS",
            results=[{"rule": "STOP", "pass": True,
                      "value": float(low), "limit": float(stop),
                      "detail": f"stop {stop} hit by low {low} on "
                                f"{bar.bar_date.isoformat()}"}],
            price_snapshot={"stop": str(stop), "low": str(low),
                            "open": str(Decimal(bar.open)),
                            "bar_date": bar.bar_date.isoformat(),
                            "fill_price": str(price), "fx_to_aud": str(fx),
                            "breaker": breaker.value})
        order_id = session.execute(text(
            "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, "
            " broker, side, qty, order_type, state, created_at) "
            "VALUES (:p, :a, :c, 'paper', 'sell', :q, 'stop', 'pending_submit', :t) "
            "RETURNING id"),
            {"p": lineage.proposal_id, "a": lineage.approval_id,
             "c": UUID(check_id), "q": qty, "t": now}).scalar_one()
        audit.append(event_type="order.state_changed", entity_type="order",
                     entity_id=str(order_id), actor_type="dcp",
                     actor_id="stop_exit_engine",
                     payload={"order_id": str(order_id), "from": None,
                              "to": "pending_submit"})
        audit.append(event_type="position.stop_hit", entity_type="position",
                     entity_id=str(p.id), actor_type="dcp",
                     actor_id="stop_exit_engine",
                     payload={"position_id": str(p.id), "symbol": p.symbol,
                              "stop": str(stop), "low": str(low),
                              "bar_date": bar.bar_date.isoformat(),
                              "order_id": str(order_id),
                              "fill_price": str(price)})
        fill = Fill(fill_date=bar.bar_date, fill_qty=qty, fill_price=price,
                    fees=Decimal(0), fx_to_aud=fx, decision_price=stop,
                    shortfall_bps=sf, executed_at=opens_at)
        report = _record_fill(session, clock, _order_row(session, order_id), fill)
        reports.append(StopExitReport(
            position_id=str(p.id), order_id=str(order_id),
            execution_id=report.execution_id, symbol=p.symbol,
            fill_date=bar.bar_date, fill_price=price, qty=qty,
            shortfall_bps=sf))
    return tuple(reports)


# ------------------------------------------------------ discretionary close

def close_position(session: Session, clock: Clock, *, position_id: str,
                   reason: str) -> ProposalResult:
    """Human-initiated exit ahead of the stop: an EXIT proposal in
    'pending_approval' that flows through the normal approve() -> sell order
    -> next-open settle path. The risk check is a PASS by construction —
    exits REDUCE risk, so L1-L11 buy-side validation does not apply (module
    docstring) — but the human click is still required: only stops are
    pre-authorized. The reason is audited on proposal.created."""
    _lifecycle_lock(session)
    now = clock.now()
    on = now.date()
    pid = UUID(position_id)
    pos = session.execute(text(
        "SELECT p.id, p.qty, p.current_stop, p.closed_at, p.thesis_memo_id, "
        "       i.id AS iid, i.symbol, i.market, i.currency "
        "FROM trading.positions p JOIN market.instruments i ON i.id = p.instrument_id "
        "WHERE p.id = :p FOR UPDATE OF p"), {"p": pid}).first()
    if pos is None:
        raise ValueError(f"unknown position {position_id}")
    if pos.closed_at is not None or int(pos.qty) <= 0:
        raise ValueError(f"position {position_id} is closed — nothing to exit")
    if pos.thesis_memo_id is None:
        raise RuntimeError(f"position {position_id} has no thesis memo — cannot "
                           "build an exit proposal without evidence lineage")
    in_flight = session.execute(text(
        "SELECT 1 FROM trading.trade_proposals tp "
        "LEFT JOIN trading.orders o ON o.proposal_id = tp.id "
        "WHERE tp.instrument_id = :iid "
        "  AND ((tp.action = 'exit' AND tp.state IN ('pending_approval','approved')) "
        "       OR (o.side = 'sell' AND o.state IN ('pending_submit','submitted'))) "
        "LIMIT 1"), {"iid": pos.iid}).first()
    if in_flight is not None:
        raise ValueError(f"an exit for {pos.symbol} is already in flight — "
                         "a second sell of the same shares is not representable")

    lineage = _entry_lineage(session, pos.id)
    close = _latest_close(session, pos.iid, on)   # fail-closed mark of THIS name
    fx = fx_to_aud(session, pos.currency, on)
    qty = int(pos.qty)
    stop = Decimal(pos.current_stop) if pos.current_stop is not None else close
    value_aud = (Decimal(qty) * close * fx).quantize(_CENT)
    breaker = _breaker_fold(_snapshot_navs(session), _confirmed_clearances(session))

    # 'risk_review' first: the pending_approval_requires_check constraint
    # (Doc 04 §2.1) forbids awaiting approval before the check row exists.
    proposal_id = session.execute(text(
        "INSERT INTO trading.trade_proposals "
        "(instrument_id, market, action, committee_memo_id, signal_ids, entry_price, "
        " stop_loss, target_price, position_size, position_value_aud, state, "
        " expires_at, created_at) "
        "VALUES (:iid, :mkt, 'exit', :memo, :sids, :entry, :stop, :target, :qty, "
        "        :value, 'risk_review', :exp, :ca) RETURNING id"),
        {"iid": pos.iid, "mkt": pos.market, "memo": pos.thesis_memo_id,
         "sids": lineage.signal_ids, "entry": close, "stop": stop,
         "target": close, "qty": qty, "value": value_aud,
         "exp": now + PROPOSAL_TTL, "ca": now}).scalar_one()
    check_id = _persist_static_check(
        session, clock, proposal_id=proposal_id, kind="proposal", verdict="PASS",
        results=[{"rule": "EXIT", "pass": True, "value": None, "limit": None,
                  "detail": f"risk-reducing: closes {qty} {pos.symbol}"}],
        price_snapshot={"entry_price": str(close), "stop_price": str(stop),
                        "fx_to_aud": str(fx), "breaker": breaker.value})
    session.execute(text(
        "UPDATE trading.trade_proposals SET state = 'pending_approval', "
        "risk_check_id = :c WHERE id = :p"),
        {"c": UUID(check_id), "p": proposal_id})

    _audit(session, clock).append(
        event_type="proposal.created", entity_type="proposal",
        entity_id=str(proposal_id), actor_type="human", actor_id="principal",
        payload={"proposal_id": str(proposal_id), "symbol": pos.symbol,
                 "action": "exit", "position_id": position_id, "qty": qty,
                 "reason": reason, "state": "pending_approval",
                 "expires_at": (now + PROPOSAL_TTL).isoformat()})
    return ProposalResult(
        proposal_id=str(proposal_id), state="pending_approval", verdict="PASS",
        risk_check_id=check_id, qty=qty, failures=())
