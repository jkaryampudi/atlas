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
- Trigger bar — SUPERSEDED 2026-07-13 by board memo 2026-07 item 6. The
  original resolution scanned only the LATEST ingested bar and held that "a
  dip that healed before the scan ran is a missed stop, recorded nowhere" —
  the daily cadence owned catching each bar as it landed. The board overruled
  that design: a missed cycle (sleeping machine, failed run) followed by a
  recovering price silently skipped a stop a live broker would have filled,
  and the hole opened exactly when ops were flaky. Every run now scans ALL
  bars strictly after the position's scan floor (next bullet) up to the
  clock's UTC date, oldest first, and fires on the FIRST bar whose low
  touched the stop — filled at min(stop, that bar's open), stamped with that
  bar's session open and that bar date's OWN FX. No scan bookmark is
  persisted: deriving "last scanned" from a prior run's clock date is
  exactly the broken behaviour, so each run re-derives the full window
  (O(bars since entry) per position, trivial at daily cadence) and
  self-heals any gap; the idempotency rule below plus closed_at keep
  re-scans from double-firing.
- Scan floor: the position's LATEST tax-lot acquisition date — equal to the
  entry date for fresh positions. The entry day's own bar can never
  re-trigger (its low may predate the fill), and the same logic covers
  add-ons: _record_fill's tighten-only stop merge can RAISE the stop at an
  add-on fill, and bars before that add-on traded under the OLD lower stop —
  evaluating them against the raised stop would fabricate fills no broker
  made. A breach of the old level that a missed cycle failed to catch before
  an add-on is deliberately not reconstructed (the old stop is not durably
  stored); protection resumes at the current stop from the add-on forward.
  Lot-less positions fall back to opened_at (they could never fire anyway —
  _entry_lineage fails closed without a lot trail).
- Catch-up visibility (board memo 2026-07 item 6): when the firing bar is
  older than the newest bar in the window, the position.stop_hit payload
  carries catch_up=true and bars_scanned=N (the window size) — a stop that
  fired late is an ops event worth seeing. A breached stop that cannot fill
  because the breach date's FX is missing emits position.stop_scan_skipped
  with the reason instead of skipping silently.
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
- FX: the firing bar date's OWN rate (fx_on_date) or skip this position this
  run — fail closed, retry next scan, audited as position.stop_scan_skipped;
  a stale rate must never enter the immutable execution row.
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
    """Fire every protective stop any unscanned bar has hit.

    For each open position with a current_stop: scan ALL bars strictly after
    the scan floor (latest lot acquisition date; module docstring) up to the
    clock's UTC date, oldest first; on the FIRST bar whose low touched the
    stop, create the pre-authorized sell order (original entry approval +
    fresh PASS 'order_time' check) and fill it in the same transaction at
    min(stop, that bar's open) with sell-side costs, that bar's session open
    and that bar date's own FX. Re-scanning the whole window every run
    self-heals missed cycles (board memo 2026-07 item 6); a firing bar older
    than the newest bar is flagged catch_up in the audit payload. Skips are
    re-scannable: no bar yet, stop never hit, session not open per the
    injected clock, or breach-date FX missing (fail closed — audited as
    position.stop_scan_skipped — retry next run). Runs under DD3 by design —
    the breaker is exit-only, never exit-blocking (Doc 04 §5)."""
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
        "       i.market, i.currency, "
        "       (SELECT max(tl.acquired_at) FROM trading.tax_lots tl "
        "         WHERE tl.position_id = p.id) AS last_acquired_at "
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
        # scan floor: the latest lot acquisition date (== the entry date for
        # fresh positions) — bars at/before it may predate the fill or the
        # add-on's tighten-only raised stop and can never trigger (module
        # docstring); lot-less rows fall back to opened_at (they could never
        # fire anyway: _entry_lineage fails closed)
        floor_date = (p.last_acquired_at.astimezone(UTC).date()
                      if p.last_acquired_at is not None
                      else p.opened_at.astimezone(UTC).date())
        bars = session.execute(text(
            "SELECT bar_date, open, low FROM market.price_bars_daily "
            "WHERE instrument_id = :iid AND source = :src "
            "  AND bar_date <= :today AND bar_date > :floor "
            "  AND open IS NOT NULL AND low IS NOT NULL "
            "ORDER BY bar_date"),
            {"iid": p.iid, "src": PRICE_SOURCE, "today": today,
             "floor": floor_date}).all()
        if not bars:
            continue                    # no post-entry bar yet
        stop = Decimal(p.current_stop)
        # board memo 2026-07 item 6: fire on the FIRST breach in the whole
        # unscanned window, never just the latest bar — a missed cycle plus
        # a healed dip must not become a silently skipped stop
        bar = next((b for b in bars if Decimal(b.low) <= stop), None)
        if bar is None:
            continue                    # stop not hit on any unscanned bar
        low = Decimal(bar.low)
        opens_at = session_open_utc(p.market, bar.bar_date)
        if opens_at > now:
            continue                    # session not open per the injected clock
        fx = fx_on_date(session, p.currency, bar.bar_date)
        if fx is None:
            # fail closed, but LOUDLY: the stop is breached and cannot fill
            # until the breach date's own FX lands — an ops condition, not a
            # silent skip (board memo 2026-07 item 6); retry next run
            audit.append(event_type="position.stop_scan_skipped",
                         entity_type="position", entity_id=str(p.id),
                         actor_type="dcp", actor_id="stop_exit_engine",
                         payload={"position_id": str(p.id), "symbol": p.symbol,
                                  "reason": "fx_missing_for_breach_date",
                                  "currency": p.currency,
                                  "bar_date": bar.bar_date.isoformat(),
                                  "stop": str(stop), "low": str(low)})
            continue
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
        hit_payload: dict[str, Any] = {
            "position_id": str(p.id), "symbol": p.symbol,
            "stop": str(stop), "low": str(low),
            "bar_date": bar.bar_date.isoformat(),
            "order_id": str(order_id), "fill_price": str(price)}
        if bar.bar_date < bars[-1].bar_date:
            # catch-up fire: the breach sat on a PAST bar while newer bars
            # exist — a missed cycle was healed; flag it for ops (board memo
            # 2026-07 item 6). bars_scanned = the window size this run saw.
            hit_payload["catch_up"] = True
            hit_payload["bars_scanned"] = len(bars)
        audit.append(event_type="position.stop_hit", entity_type="position",
                     entity_id=str(p.id), actor_type="dcp",
                     actor_id="stop_exit_engine", payload=hit_payload)
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
