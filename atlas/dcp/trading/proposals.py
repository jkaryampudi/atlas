"""Trade-proposal lifecycle (Doc 05 §5 state machine, Doc 06 §3 contract,
Doc 04 §2 veto + approval-time re-check). Pure deterministic compute plane:
no agent import (two-plane wall), no wall clock (injectable Clock only), and
every material transition lands in audit.decision_events.

The committee memo is referenced by ID only — it is evidence produced upstream
(Principle 1: no trade without evidence, enforced by the NOT NULL FK); nothing
in here reads or interprets agent output.

Lifecycle implemented here:
  build_proposal -> 'pending_approval' (risk PASS) | 'rejected' (risk FAIL, terminal)
  approve        -> fresh re-check -> 'approved' + order | 'voided' (RISK_RECHECK_FAILED)
  reject         -> 'rejected'
  expire_stale   -> 'expired' past the 24h TTL
  settle_orders  -> PaperBroker next-session-open fill -> 'executed'
  snapshot       -> trading.portfolio_snapshots row via compute_snapshot

Documented resolutions (Doc ambiguities, resolved conservatively):
- Empty book: cash seeds at SEED_CASH_AUD = A$100,000 — the hypothetical paper
  bankroll (CLAUDE.md preamble); cash is thereafter a pure ledger over
  trading.executions, so replays are exact.
- Breaker level is the LATCHED fold of engine.next_breaker_state over the
  trading.portfolio_snapshots NAV history (Doc 04 §5): DD2/DD3 do not clear
  on NAV recovery. The ONLY step down is a confirmed risk.breaker_clearances
  row (the dual-confirmation human action, atlas.dcp.risk.clearance): a
  clearance confirmed between two NAV points makes the fold evaluate that
  step with human_cleared=True, which steps the latch down to the COMPUTED
  target — still DD2 if the drawdown is live at DD2 depth (you clear a
  latched memory of a drawdown, never a live one). A clearance confirmed
  after every persisted snapshot applies at the next evaluated point: the
  live nav_now point in _latched_breaker, or a re-evaluation of the LAST
  persisted point when the fold runs over history alone (the exit paths and
  clearance.py never mark the book, so the last persisted drawdown state is
  the honest evaluation point). With zero clearance rows the fold is
  behaviorally identical to the pre-clearance latch. Fail-closed by design.
- Worst-case pro-forma (Doc 04 §3): pending 'pending_submit'/'submitted'
  orders count as holdings at their proposal entry price, and their cost is
  deducted from pro-forma cash.
- Fail-closed valuations: a held instrument without a vendor close (or with
  only a close older than 7 calendar days), or a currency without an FX rate,
  raises; a position without a stop counts its FULL value as open risk (L7);
  fewer than 20 sessions of volume history means adv_20d = 0 (L10 fails).
- A §4 sizing rejection is persisted as a FAIL risk check (rule 'SIZING') and
  the proposal lands 'rejected' — sizing is part of the risk policy.
- reject() records an approvals row whose approval_time_risk_check_id is the
  proposal-time check (the schema requires a check reference; only APPROVE
  demands a fresh one, Doc 05 §7); the reason lives in the audit payload
  (approvals has no reason column).
- approve() on a stale proposal transitions it to 'expired' and returns the
  Doc 06 §3.3 PROPOSAL_EXPIRED code; a voided re-check returns
  RISK_RECHECK_FAILED with itemised results — neither raises.
- risk_checks.portfolio_snapshot_id references the latest PERSISTED snapshot
  (possibly NULL on an empty book); the fresh pro-forma state the check was
  actually evaluated against is recorded in price_snapshot.
- Every book-mutating entrypoint takes one pg advisory xact lock, so builds,
  approvals and settlements serialise: two concurrent approvals can never
  both pass a re-check against a book that excludes the other.
- An add-on fill can only TIGHTEN the position stop (max of old and new for
  longs) — a looser stop would expand aggregate open risk beyond what L7
  approved. NOTE: add-ons are currently unreachable through build_proposal
  anyway, because L8 correlation with the held symbol itself fails closed to
  1; whether to allow pyramiding is an open policy question for the principal.
- Stuck orders (fill-date bar permanently missing) never fill at a later
  session's price — cancel_order() is the human escape hatch (order ->
  'cancelled', proposal -> 'voided').

Documented resolutions — sell settlement (Phase 5 exits, Doc 04 §5/§14):
- Reducing a position leaves avg_cost untouched (realised P&L lives in the
  tax lots: proceeds vs cost per lot); a reduction to zero closes the
  position with closed_at = the fill's executed_at.
- Lots dispose FIFO by acquired_at (created_at, id as tiebreaks). A partial
  disposal SPLITS the lot: the ORIGINAL row becomes the disposed slice (qty
  taken, cost pro-rated by qty and quantized to cents, disposed_at +
  proceeds_aud set) and a NEW row carries the residual qty with the residual
  cost = original minus disposed, so cents are conserved exactly.
- Per-lot proceeds are qty x fill price x fill FX, quantized to cents PER
  LOT; the position.closed/position.reduced payloads carry the lot-booked
  total, which may differ from the unquantized execution value by sub-cent
  rounding — the cash ledger reads executions, never lots.
- approve() branches on the proposal's action: action='exit' skips
  engine.validate entirely — L1-L11 gate risk ADDED to the book and an exit
  only releases risk; blocking it under DD2/DD3 would invert Doc 04 §5 (DD3
  is exit-ONLY, never exit-blocking). The fresh approval-time check instead
  re-verifies the EXIT premise (position still open at the approved qty) and
  records the breaker statement; a closed/resized position voids the
  approval with the same RISK_RECHECK_FAILED shape as the buy path.
- Pending SELL orders are excluded from the worst-case pro-forma pending
  block in _build_book: the worst case for a sell is that it does NOT fill,
  and the still-open position is already counted as a holding.
- settle_orders lineage rule: the referenced check must be a PASS and either
  (check_kind='approval_time' AND proposal 'approved') — the human-approved
  buy/exit path — or (check_kind='order_time' AND proposal 'executed') — a
  stop-exit order, whose proposal is the already-executed ENTRY proposal.
  Stop-exit orders are created and filled in one transaction by
  atlas.dcp.trading.exits, so one arriving at settle unfilled is unreachable
  through the lifecycle; if one exists anyway it is NOT an integrity breach
  (its lineage is genuine) and it fills as a plain sell at the next session
  open — cancel_order stays the human escape hatch if that is unwanted.
- _record_fill transitions the proposal to 'executed' (and emits
  proposal.executed) only when it is not already there: a stop-exit fill
  references the entry proposal, which the entry fill already executed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.execution.paper import PRICE_SOURCE, Broker, Fill, OrderTicket, PaperBroker, fx_to_aud
from atlas.dcp.portfolio.snapshot import Holding, compute_snapshot
from atlas.dcp.risk.approval_recheck import recheck_at_approval
from atlas.dcp.risk.correlations import correlations_with_existing
from atlas.dcp.risk.engine import (
    BreakerLevel,
    HoldingRisk,
    Limits,
    PortfolioState,
    RiskCheck,
    RuleResult,
    TradeProposal,
    drawdown,
    load_active_limit_set,
    next_breaker_state,
    size_position,
    validate,
)
from atlas.dcp.risk.factor_overlap import FactorLoadings, check_factor_overlap
from atlas.dcp.risk.stress import StressHolding, stress_marginal_gate
from atlas.dcp.risk.vol_target import gross_step_gate

SEED_CASH_AUD = Decimal("100000")   # hypothetical A$100k paper bankroll (CLAUDE.md)
PROPOSAL_TTL = timedelta(hours=24)  # Doc 05 §5: expires_at = created + 24h
_CENT = Decimal("0.01")
_PCT = Decimal("0.0001")
_QTY6 = Decimal("0.000001")


# ------------------------------------------------------------------- results

@dataclass(frozen=True)
class ProposalResult:
    proposal_id: str
    state: str                  # 'pending_approval' | 'rejected'
    verdict: str                # 'PASS' | 'FAIL'
    risk_check_id: str
    qty: int
    failures: tuple[str, ...]   # failing rule names, empty on PASS


@dataclass(frozen=True)
class ApprovalOutcome:
    """Doc 06 §3.2 minus HTTP: 'approved', or the §3.3 codes
    RISK_RECHECK_FAILED / PROPOSAL_EXPIRED as structured results."""
    status: str
    proposal_id: str
    order_id: str | None
    risk_check_id: str | None   # the FRESH approval-time check when one ran
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class FillReport:
    order_id: str
    execution_id: str
    fill_date: date
    fill_price: Decimal
    shortfall_bps: Decimal


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_id: str
    nav_aud: Decimal
    cash_aud: Decimal
    open_risk_pct: Decimal


# ------------------------------------------------------------ internal views

@dataclass(frozen=True)
class _Instrument:
    id: UUID
    symbol: str
    market: str
    instrument_type: Literal["stock", "etf", "adr"]  # DB CHECK-enforced
    sector_gics: str
    currency: str
    india_exposed: bool


@dataclass(frozen=True)
class _Book:
    """Worst-case pro-forma inputs for engine.validate (Doc 04 §3)."""
    state: PortfolioState
    breaker: BreakerLevel
    snapshot_id: UUID | None    # latest persisted snapshot, for the check row
    existing_symbols: tuple[str, ...]


def _audit(session: Session, clock: Clock) -> PostgresAuditLog:
    return PostgresAuditLog(session, clock)


def _lifecycle_lock(session: Session) -> None:
    """One portfolio-wide advisory xact lock serialises every book-mutating
    entrypoint: without it, two concurrent approvals each re-check against a
    book that excludes the other's order (a race-shaped L3/L5/L9 bypass), and
    two settle runs can double-fill an order."""
    session.execute(text(
        "SELECT pg_advisory_xact_lock(hashtext('atlas.trading.lifecycle'))"))


def _india_exposed(market: str, economic_exposure: Sequence[str] | None) -> bool:
    return market == "IN" or "IN" in (economic_exposure or [])


_INSTRUMENT_SQL = ("SELECT id, symbol, market, instrument_type, sector_gics, "
                   "economic_exposure, currency FROM market.instruments ")


def _instrument_from(row: Any) -> _Instrument:
    return _Instrument(
        id=row.id, symbol=row.symbol, market=row.market,
        instrument_type=row.instrument_type,
        sector_gics=row.sector_gics or "Unknown",   # NULL sector = own bucket
        currency=row.currency,
        india_exposed=_india_exposed(row.market, row.economic_exposure))


def _load_instrument(session: Session, symbol: str) -> _Instrument:
    rows = session.execute(text(
        _INSTRUMENT_SQL + "WHERE symbol = :s AND is_active"), {"s": symbol}).all()
    if len(rows) != 1:
        raise ValueError(f"expected exactly one active instrument for {symbol!r}, "
                         f"found {len(rows)}")
    return _instrument_from(rows[0])


def _load_instrument_by_id(session: Session, instrument_id: UUID) -> _Instrument:
    row = session.execute(text(_INSTRUMENT_SQL + "WHERE id = :i"),
                          {"i": instrument_id}).one()
    return _instrument_from(row)


MAX_MARK_STALENESS = timedelta(days=7)   # a week-old close is not a mark


def _latest_close(session: Session, instrument_id: UUID, on: date) -> Decimal:
    """Most recent vendor close on or before `on`. FAIL-CLOSED both ways: a
    held instrument that cannot be marked raises, and so does one whose
    freshest close is more than MAX_MARK_STALENESS old — never a guess."""
    row = session.execute(text(
        "SELECT close, bar_date FROM market.price_bars_daily "
        "WHERE instrument_id = :iid AND source = :src AND bar_date <= :d "
        "  AND close IS NOT NULL ORDER BY bar_date DESC LIMIT 1"),
        {"iid": instrument_id, "src": PRICE_SOURCE, "d": on}).first()
    if row is None:
        raise RuntimeError(f"no vendor close for instrument {instrument_id} "
                           f"on or before {on} — cannot mark the book")
    if on - row.bar_date > MAX_MARK_STALENESS:
        raise RuntimeError(f"freshest close for instrument {instrument_id} is "
                           f"{row.bar_date}, more than {MAX_MARK_STALENESS.days} "
                           f"days before {on} — stale data cannot mark the book")
    return Decimal(row.close)


def _adv_20d(session: Session, instrument_id: UUID, on: date) -> int:
    """20-session average daily volume; 0 unless a FULL 20 sessions exist —
    a thin history must not overstate liquidity (L10 fails closed)."""
    row = session.execute(text(
        "SELECT avg(v), count(*) FROM (SELECT volume AS v "
        "FROM market.price_bars_daily "
        "WHERE instrument_id = :iid AND source = :src AND bar_date <= :d "
        "  AND volume IS NOT NULL ORDER BY bar_date DESC LIMIT 20) w"),
        {"iid": instrument_id, "src": PRICE_SOURCE, "d": on}).one()
    avg, n = row
    return int(avg) if avg is not None and n >= 20 else 0


def _ledger_cash(session: Session) -> Decimal:
    """Cash as a pure ledger: A$100k seed minus buy costs plus sell proceeds,
    AUD-translated at each execution's recorded rate (fees included)."""
    cash = SEED_CASH_AUD
    rows = session.execute(text(
        "SELECT o.side, e.fill_qty, e.fill_price, e.fees, e.fx_rate_used "
        "FROM trading.executions e JOIN trading.orders o ON o.id = e.order_id")).all()
    for r in rows:
        gross = Decimal(r.fill_qty) * Decimal(r.fill_price) * Decimal(r.fx_rate_used)
        fees = Decimal(r.fees) * Decimal(r.fx_rate_used)
        cash += (gross - fees) if r.side == "sell" else -(gross + fees)
    return cash


# Timestamp of the live (not yet persisted) NAV point in _latched_breaker:
# later than any snapshot, so every confirmed clearance sorts before it.
# clearance.py stamps confirmed_at from the injected clock, never the future.
_LIVE_POINT = datetime.max.replace(tzinfo=UTC)


def _breaker_fold(points: Sequence[tuple[datetime, Decimal]],
                  clearances: Sequence[datetime] = ()) -> BreakerLevel:
    """Chronological latch fold (Doc 04 §5): DD2/DD3, once entered, survive
    NAV recovery — engine.next_breaker_state only steps them down through the
    dual-confirmed human action. Each confirmed clearance instant is consumed
    by the first (as_of, nav) point at or after it: that step evaluates with
    human_cleared=True and lands on the COMPUTED target for its drawdown
    (still DD2 if the drawdown is live at DD2 depth). Clearances after every
    point re-evaluate the LAST point — the latch steps down at the last known
    drawdown state without waiting for the next snapshot (module docstring).
    With no clearances this is exactly the original latch fold."""
    level, hwm = BreakerLevel.NONE, Decimal(0)
    nav: Decimal | None = None
    remaining = sorted(clearances)
    i = 0
    for as_of, nav in points:
        cleared = False
        while i < len(remaining) and remaining[i] <= as_of:
            cleared, i = True, i + 1
        hwm = max(hwm, nav)
        level = next_breaker_state(level, drawdown(nav, hwm),
                                   human_cleared=cleared)
    if i < len(remaining) and nav is not None:
        level = next_breaker_state(level, drawdown(nav, hwm), human_cleared=True)
    return level


def _snapshot_navs(session: Session) -> list[tuple[datetime, Decimal]]:
    return [(r.as_of, Decimal(r.nav_aud)) for r in session.execute(text(
        "SELECT as_of, nav_aud FROM trading.portfolio_snapshots ORDER BY as_of"))]


def _confirmed_clearances(session: Session) -> list[datetime]:
    """Confirmed dual-confirmation clearance instants, ascending (Doc 04 §5:
    resumption from DD2/DD3). Pending requests (confirmed_at NULL) do not
    move the fold — only confirmation B does."""
    return [r.confirmed_at for r in session.execute(text(
        "SELECT confirmed_at FROM risk.breaker_clearances "
        "WHERE confirmed_at IS NOT NULL ORDER BY confirmed_at"))]


def _latched_breaker(session: Session, nav_now: Decimal) -> BreakerLevel:
    return _breaker_fold([*_snapshot_navs(session), (_LIVE_POINT, nav_now)],
                         _confirmed_clearances(session))


def _build_book(session: Session, clock: Clock) -> _Book:
    """Worst-case pro-forma PortfolioState from trading.positions plus pending
    approved-but-unfilled orders, marked at latest vendor closes and FX."""
    on = clock.now().date()
    positions = session.execute(text(
        "SELECT p.qty, p.current_stop, p.is_core, i.id AS iid, i.symbol, "
        "       i.sector_gics, i.market, i.economic_exposure, i.currency "
        "FROM trading.positions p JOIN market.instruments i ON i.id = p.instrument_id "
        "WHERE p.closed_at IS NULL AND p.qty > 0")).all()

    holdings: list[HoldingRisk] = []
    marks: list[Holding] = []
    rates: dict[str, Decimal] = {"AUD": Decimal(1)}
    for p in positions:
        rates.setdefault(p.currency, fx_to_aud(session, p.currency, on))
        close = _latest_close(session, p.iid, on)
        fx = rates[p.currency]
        value = Decimal(p.qty) * close * fx
        # ADR-0014: a no-stop position carries NO stop-out distance -> risk None;
        # the engine (not the book-builder) then zeroes a core holding and
        # FAILS CLOSED a satellite (no stop = full value at risk). is_core is
        # the positive marker (migration 0023) that keeps those two apart.
        risk: Decimal | None
        if p.current_stop is None:
            risk = None
        else:
            risk = max(Decimal(0), (close - Decimal(p.current_stop))) * Decimal(p.qty) * fx
        holdings.append(HoldingRisk(
            symbol=p.symbol, value_aud=value,
            sector_gics=p.sector_gics or "Unknown",
            india_exposed=_india_exposed(p.market, p.economic_exposure),
            currency=p.currency, risk_to_stop_aud=risk, is_core=p.is_core))
        marks.append(Holding(symbol=p.symbol, qty=p.qty, currency=p.currency,
                             last_price=close))

    cash = _ledger_cash(session)
    nav = compute_snapshot(cash_aud=cash, holdings=marks, fx_to_aud=rates).nav_aud

    # Worst case is one-sided: pending BUYS count as if they all fill (adds
    # exposure + reserves cash); pending SELLS count as if they do NOT fill —
    # the still-open position is already in `holdings` above, so including the
    # sell would double-count or, worse, pre-release risk it has not released.
    pending_cost = Decimal(0)
    pending = session.execute(text(
        "SELECT o.qty, o.created_at, tp.entry_price, tp.stop_loss, tp.origin, "
        "       i.id AS iid, i.symbol, i.sector_gics, i.market, "
        "       i.economic_exposure, i.currency "
        "FROM trading.orders o "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE o.state IN ('pending_submit','submitted') AND o.side = 'buy'")).all()
    for o in pending:
        rates.setdefault(o.currency, fx_to_aud(session, o.currency, on))
        fx = rates[o.currency]
        cost = Decimal(o.qty) * Decimal(o.entry_price) * fx
        pending_cost += cost
        # Mirror the holdings rule above (a core_allocation order carries a NULL
        # stop and ZERO stop-out risk per ADR-0014/migration 0022; an agent order
        # carries a NOT-NULL stop): NULL stop -> risk None (the engine zeroes a
        # core holding and FAILS CLOSED a stopless satellite), is_core from
        # origin. A hardcoded is_core=False + Decimal(NULL stop) crashed the whole
        # pro-forma book the moment a core order sat in pending_submit — the
        # comment here once ASSERTED an agent-only invariant the SQL never
        # enforced (adversarial review 2026-07-18; pre-existing at HEAD).
        pending_risk: Decimal | None
        if o.stop_loss is None:
            pending_risk = None
        else:
            pending_risk = ((Decimal(o.entry_price) - Decimal(o.stop_loss))
                            * Decimal(o.qty) * fx)
        holdings.append(HoldingRisk(
            symbol=o.symbol, value_aud=cost,
            sector_gics=o.sector_gics or "Unknown",
            india_exposed=_india_exposed(o.market, o.economic_exposure),
            currency=o.currency, risk_to_stop_aud=pending_risk,
            is_core=(o.origin == "core_allocation")))

    opened_today = session.execute(text(
        "SELECT count(*) FROM trading.positions "
        "WHERE opened_at IS NOT NULL AND (opened_at AT TIME ZONE 'UTC')::date = :d"),
        {"d": on}).scalar_one()
    pending_new_today = session.execute(text(
        "SELECT count(*) FROM trading.orders o "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "WHERE o.state IN ('pending_submit','submitted') AND o.side = 'buy' "
        "  AND (o.created_at AT TIME ZONE 'UTC')::date = :d "
        "  AND NOT EXISTS (SELECT 1 FROM trading.positions p "
        "                  WHERE p.instrument_id = tp.instrument_id "
        "                    AND p.closed_at IS NULL)"), {"d": on}).scalar_one()

    snap = session.execute(text(
        "SELECT id, nav_aud FROM trading.portfolio_snapshots "
        "ORDER BY as_of DESC LIMIT 1")).first()
    breaker = _latched_breaker(session, nav)

    state = PortfolioState(
        nav_aud=nav, cash_aud=cash - pending_cost, holdings=tuple(holdings),
        new_positions_today=int(opened_today) + int(pending_new_today))
    return _Book(state=state, breaker=breaker,
                 snapshot_id=snap.id if snap is not None else None,
                 existing_symbols=tuple(h.symbol for h in holdings))


def _persist_check(session: Session, clock: Clock, *, proposal_id: UUID,
                   limits: Limits, book: _Book, check: RiskCheck, kind: str,
                   price_snapshot: dict[str, Any]) -> str:
    """One risk.risk_checks row per evaluation — itemised, never summarised.
    Doc 05 §4 result shape: {rule, value, limit, pass} (+ the exact detail)."""
    results = [{"rule": r.rule, "pass": r.passed,
                "value": float(r.value) if r.value is not None else None,
                "limit": float(r.limit) if r.limit is not None else None,
                "detail": r.detail}
               for r in check.results]
    price_snapshot = {**price_snapshot, "breaker": check.breaker.value}
    check_id = session.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, limit_set_version, "
        " portfolio_snapshot_id, price_snapshot, results, verdict, check_kind, created_at) "
        "VALUES (:p, :v, :ps, CAST(:pj AS jsonb), CAST(:r AS jsonb), :verdict, :k, :ca) "
        "RETURNING id"),
        {"p": proposal_id, "v": limits.version, "ps": book.snapshot_id,
         "pj": json.dumps(price_snapshot), "r": json.dumps(results),
         "verdict": "PASS" if check.passed else "FAIL", "k": kind,
         "ca": clock.now()}).scalar_one()
    _audit(session, clock).append(
        event_type="risk.check.completed", entity_type="risk_check",
        entity_id=str(check_id), actor_type="dcp", actor_id="risk_engine",
        payload={"risk_check_id": str(check_id), "proposal_id": str(proposal_id),
                 "check_kind": kind, "verdict": "PASS" if check.passed else "FAIL",
                 "failures": [r.rule for r in check.failures()]})
    return str(check_id)


def _persist_static_check(session: Session, clock: Clock, *, proposal_id: UUID,
                          kind: str, verdict: str, results: list[dict[str, Any]],
                          price_snapshot: dict[str, Any]) -> str:
    """risk.risk_checks row for a check that did NOT run engine.validate —
    the EXIT/STOP rules of the exit path (Doc 04 §5: exits release risk, so
    L1-L11 buy-side validation does not apply). limit_set_version stays NULL:
    no limit set was consulted, and the row must not claim otherwise. Same
    itemised result shape and audit event as _persist_check."""
    check_id = session.execute(text(
        "INSERT INTO risk.risk_checks (proposal_id, price_snapshot, results, "
        " verdict, check_kind, created_at) "
        "VALUES (:p, CAST(:pj AS jsonb), CAST(:r AS jsonb), :verdict, :k, :ca) "
        "RETURNING id"),
        {"p": proposal_id, "pj": json.dumps(price_snapshot),
         "r": json.dumps(results), "verdict": verdict, "k": kind,
         "ca": clock.now()}).scalar_one()
    _audit(session, clock).append(
        event_type="risk.check.completed", entity_type="risk_check",
        entity_id=str(check_id), actor_type="dcp", actor_id="risk_engine",
        payload={"risk_check_id": str(check_id), "proposal_id": str(proposal_id),
                 "check_kind": kind, "verdict": verdict,
                 "failures": [r["rule"] for r in results if not r["pass"]]})
    return str(check_id)


def _fresh_proposal_inputs(session: Session, clock: Clock, inst: _Instrument, *,
                           qty: int, entry_price: Decimal, stop_price: Decimal,
                           book: _Book) -> TradeProposal:
    """Engine TradeProposal against CURRENT prices/FX/correlations — used both
    at build time and for the approval-time re-check (Doc 04 §2.2)."""
    on = clock.now().date()
    return TradeProposal(
        symbol=inst.symbol, side="BUY", qty=qty,
        entry_price=entry_price, stop_price=stop_price,
        fx_to_aud=fx_to_aud(session, inst.currency, on),
        instrument_type=inst.instrument_type, sector_gics=inst.sector_gics,
        india_exposed=inst.india_exposed, currency=inst.currency,
        adv_20d=_adv_20d(session, inst.id, on),
        corr_with_existing=correlations_with_existing(
            session, inst.symbol, list(book.existing_symbols), end=on))


# ---------------------------------------- policy overlay (§7/§11/§12 wiring)
#
# Risk-wiring bundle (Principal, 2026-07-18): every protection the signed risk
# policy claims has a LIVE call site. stress_marginal_gate (§7),
# check_factor_overlap (§12) and gross_step_gate (§11 Tier 1) were built and
# unit-tested with zero call sites outside their own tests; build_proposal now
# runs all three against the same worst-case pro-forma book engine.validate
# sees, and their RuleResults join the persisted risk-check rows — a FAIL
# fails the check exactly like any L-rule (invariant 3: risk FAIL is
# terminal). Documented scope decisions, stated honestly:
#
# - STRESS inputs: symbol/value/sector/india/currency come straight from the
#   book (real data). rate_beta_per_100bp defaults to 0 for every holding —
#   the §7 beta table is data that does not exist yet, and per-name betas are
#   never invented. This default CANNOT soften the gated number: the policy
#   gate prices only the broad-equity-crash scenario, which has no rate leg.
#   The rates_shock scenario stays out of gating until a real beta table lands.
# - STRESS reachability: for an unlevered long-only book, holdings/NAV <= 1,
#   so the crash loss is bounded by 25% x india_weight + 20% x rest < 25% of
#   NAV whenever the book is a legal lifecycle state — the FAIL branch binds
#   only on a book already in breach of the pro-forma cash identity (defense
#   in depth) or if the scenario library ever deepens. Wired regardless: the
#   policy claims the gate, so the gate runs and records its numbers.
# - FACTOR scope (v1, honest): sector loadings are real (GICS weights from the
#   book — and unlike L3, which prices only the PROPOSAL's sector, FACTOR
#   itemises EVERY sector, so a book already over-cap in an unrelated sector
#   now fails a new buy). market_beta is the class-level equity beta of 1.0
#   for every holding — market loading == gross exposure; no per-name
#   regression betas are invented, so this arm cannot bind before a real beta
#   feed exists (gross <= 1 <= cap). momentum is sleeve-membership: 1.0 iff
#   the name is attributed to a MOMENTUM_FAMILIES strategy via the same
#   signal-lineage join bands.py and the sleeve budget use (open lots + live
#   proposals), else 0.0 — deliberately NOT a cross-sectional z-score, which
#   would need a point-in-time universe ranking at proposal time (deferred).
# - VOL semantics: gross_after = (holdings incl. pending buys + this
#   proposal) / NAV vs MAX_GROSS. step_after charges the day's cumulative
#   committed gross increase on the proposal's BUILD day: the AUD sum of
#   today's live-or-executed BUY proposals (risk_review/pending_approval/
#   approved/executed) plus this one, over NAV, vs MAX_STEP. Commitment-day
#   accounting, exactly once: a fill of yesterday's approval charged
#   yesterday; sells never credit the budget (conservative — the cap binds
#   earlier, never later). Dead states (rejected/voided/expired) release
#   their charge. Any DD breaker fails a gross increase (vol_target module
#   docstring records the deliberate DD1 strictness).
# - The overlay runs at PROPOSAL build (the §7 "pro-forma on every proposal"
#   claim). The §2.2 approval-time re-check remains engine.validate L1-L11 —
#   the overlay verdict that gated pending_approval is not re-evaluated on
#   the fresh book at approval; widening §2.2 is a follow-up, recorded here
#   so the gap is documented, never silent. A sizing rejection persists the
#   single SIZING row as before: a proposal with no size has nothing to
#   stress or load.

MOMENTUM_FAMILIES: tuple[str, ...] = ("xsmom-pit-tr",)
# §12 momentum-factor attribution: the signed strategy families whose signal
# lineage marks a name as a momentum bet. PEAD is earnings drift, not price
# momentum, and is deliberately absent. Editing this tuple is a reviewed
# change, like the caps it feeds.

_MOMENTUM_SIGNALS = ("ARRAY(SELECT s.id FROM quant.signals s "
                     "JOIN quant.strategies st ON st.id = s.strategy_id "
                     "WHERE st.family = ANY(:fams) "
                     "  AND st.state IN ('MUTANT_no_such_state'))")


def _momentum_symbols(session: Session) -> frozenset[str]:
    """Symbols currently attributed to a momentum-family strategy: open tax
    lots plus live (unfilled) proposals, through the proposal signal_ids ->
    quant.signals lineage — the exact attribution join _sleeve_committed_aud
    and bands.py use, so the factor view and the sleeve ledger cannot
    disagree about what is a momentum bet."""
    rows = session.execute(text(
        "SELECT i.symbol FROM trading.tax_lots tl "
        "JOIN trading.executions e ON e.id = tl.execution_id "
        "JOIN trading.orders o ON o.id = e.order_id "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE tl.disposed_at IS NULL AND tp.signal_ids && " + _MOMENTUM_SIGNALS +
        " UNION "
        "SELECT i.symbol FROM trading.trade_proposals tp "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE tp.state IN ('risk_review','pending_approval','approved') "
        "  AND tp.signal_ids && " + _MOMENTUM_SIGNALS),
        {"fams": list(MOMENTUM_FAMILIES)}).all()
    return frozenset(str(r.symbol) for r in rows)


def _proposal_is_momentum(session: Session, signal_refs: Sequence[str]) -> bool:
    """True iff any of the proposal's signal ids IS a momentum-family
    quant.signals row. Interim uuid5 evidence ids simply never match."""
    return session.execute(text(
        "SELECT 1 FROM quant.signals s "
        "JOIN quant.strategies st ON st.id = s.strategy_id "
        "WHERE s.id = ANY(:ids) AND st.family = ANY(:fams) "
        "  AND st.state IN ('MUTANT_no_such_state') LIMIT 1"),
        {"ids": [UUID(r) for r in signal_refs],
         "fams": list(MOMENTUM_FAMILIES)}).first() is not None


def _committed_gross_today_aud(session: Session, on: date) -> Decimal:
    """AUD gross committed on build day `on`: live-or-executed BUY proposals
    created today, at their recorded position_value_aud (commitment-day
    accounting — see the overlay block). The proposal being built is not yet
    inserted, so it is never double-counted.

    origin='core_allocation' legs are EXCLUDED, documented: the passive core
    is rebalanced by target weight through its own ADR-0014 builder (which
    never runs this gate), so charging a scheduled core top-up against the
    day-step would let the core lane silently veto the satellite lane — and
    the core bootstrap day (SPY at 55%) would block every satellite for the
    day. The step cap governs the lane it gates: every build_proposal BUY
    (origin 'agent') is charged in full. Core exposure still binds through
    the gross_after arm (core positions are holdings) and L1-L5."""
    total = session.execute(text(
        "SELECT COALESCE(sum(position_value_aud), 0) "
        "FROM trading.trade_proposals "
        "WHERE action = 'buy' AND origin != 'core_allocation' "
        "  AND state IN ('risk_review','pending_approval','approved','executed') "
        "  AND (created_at AT TIME ZONE 'UTC')::date = :d"), {"d": on}).scalar_one()
    return Decimal(total)


def _policy_overlay(session: Session, *, proposal: TradeProposal, book: _Book,
                    signal_refs: Sequence[str], on: date,
                    gross_cap: Decimal) -> tuple[RuleResult, ...]:
    """The three policy-overlay rows (STRESS §7, FACTOR §12, VOL §11) for one
    sized proposal against the same worst-case pro-forma book engine.validate
    evaluated. Pure reads; every scope decision is documented in the overlay
    block above. `gross_cap` = 1 - active L5 cash floor (the VOL ceiling
    tracks L5 — Principal 2026-07-18).

    RESIDUALS accepted at v1 land (adversarial review 2026-07-18, documented
    not hidden): (a) FACTOR's market and momentum arms cannot bind under the
    current single-family config (market loading == gross <= cap; momentum ==
    sleeve membership, never reaching the 0.5 cap) — the SECTOR arm is the
    live gating protection; the others are informational until a real beta
    feed / cross-sectional score lands. (b) This overlay gates at BUILD time;
    engine.validate's own DD gate re-runs at approval (so a DD2/DD3 that
    latches between build and approval still blocks a new position), but the
    STRESS/FACTOR/VOL rows are not themselves re-evaluated at approval. (c)
    The VOL day-step ledger is commitment-day accounting: two build-day
    cohorts filling at one session's open can realise up to 2x MAX_STEP.
    None of these permits a dangerous trade (all are over-refusal or
    audit-honesty); each is a tracked follow-up for the next revision."""
    nav = book.state.nav_aud
    stress_book = tuple(
        StressHolding(symbol=h.symbol, value_aud=h.value_aud,
                      sector_gics=h.sector_gics, india_exposed=h.india_exposed,
                      currency=h.currency)
        for h in book.state.holdings)
    stress_row = stress_marginal_gate(proposal, stress_book, nav_aud=nav)

    momentum = _momentum_symbols(session)
    factor_book = [
        FactorLoadings(symbol=h.symbol, value_aud=h.value_aud,
                       market_beta=Decimal(1), sector_gics=h.sector_gics,
                       momentum=Decimal(1 if h.symbol in momentum else 0))
        for h in book.state.holdings]
    factor_proposal = FactorLoadings(
        symbol=proposal.symbol, value_aud=proposal.cost_aud,
        market_beta=Decimal(1), sector_gics=proposal.sector_gics,
        momentum=Decimal(1 if _proposal_is_momentum(session, signal_refs) else 0))
    factor_row = check_factor_overlap(factor_proposal, factor_book, nav_aud=nav)

    gross_after = (sum((h.value_aud for h in book.state.holdings), Decimal(0))
                   + proposal.cost_aud) / nav
    step_after = (_committed_gross_today_aud(session, on)
                  + proposal.cost_aud) / nav
    vol_row = gross_step_gate(gross_after=gross_after, step_after=step_after,
                              breaker=book.breaker, gross_cap=gross_cap)
    return (stress_row, factor_row, vol_row)


# ---------------------------------------------------------------- build (§5)

def build_proposal(session: Session, clock: Clock, *, memo_id: str, symbol: str,
                   signal_refs: Sequence[str], entry_price: Decimal,
                   stop_price: Decimal, target_price: Decimal,
                   sleeve_max_qty: int | None = None) -> ProposalResult:
    """Size (Doc 04 §4), validate (L1-L11 §3), overlay (STRESS §7 / FACTOR §12
    / VOL §11 — the policy-overlay block above), persist. PASS lands the
    proposal in 'pending_approval' with the check referenced (§2.1); FAIL is
    terminal: 'rejected', check recorded, no override path.

    sleeve_max_qty (ADR-0014, set only by the bridge's sleeve budget): an OUTER
    whole-share cap the DCP applies to the §4 risk size so a strategy sleeve's
    aggregate exposure stays inside its signed envelope. It can only ever SHRINK
    the size — the risk engine still validates the capped quantity, and a
    smaller position is strictly less risk than the one §4 sized — never grow it
    past what risk allows. None leaves §4 sizing untouched (every non-sleeve
    proposal, unchanged). Callers pass a cap >= 1; the sleeve budget's
    'no whole share fits' case is a skip in the bridge, never a 0-qty proposal."""
    _lifecycle_lock(session)
    on = clock.now().date()
    limits = load_active_limit_set(session, on)   # raises before effective_from
    inst = _load_instrument(session, symbol)
    book = _build_book(session, clock)

    fx = fx_to_aud(session, inst.currency, on)
    size = size_position(
        nav_aud=book.state.nav_aud, entry_price=entry_price, stop_price=stop_price,
        fx_to_aud=fx, instrument_type=inst.instrument_type,
        adv_20d=_adv_20d(session, inst.id, on), limits=limits, breaker=book.breaker)

    if size.accepted:
        qty = (size.qty if sleeve_max_qty is None
               else min(size.qty, sleeve_max_qty))   # the cap only shrinks
        proposal = _fresh_proposal_inputs(session, clock, inst, qty=qty,
                                          entry_price=entry_price,
                                          stop_price=stop_price, book=book)
        check = validate(proposal, book.state, limits, book.breaker)
        # VOL gross ceiling TRACKS L5 (Principal 2026-07-18): 1 - active cash
        # floor. Under limit_set v2 (L5=0.10) that is 0.90, matching ADR-0014.
        gross_cap = Decimal(1) - limits.l5_min_cash_reserve
        overlay = _policy_overlay(session, proposal=proposal, book=book,
                                  signal_refs=signal_refs, on=on,
                                  gross_cap=gross_cap)
        check = RiskCheck(   # overlay FAILs fail the check like any L-rule
            passed=check.passed and all(r.passed for r in overlay),
            breaker=check.breaker, results=(*check.results, *overlay))
        value_aud = proposal.cost_aud.quantize(_CENT)
    else:  # §4 'reject if …' — sizing is risk policy, recorded as a FAIL check
        qty = size.qty
        check = RiskCheck(passed=False, breaker=book.breaker, results=(
            RuleResult("SIZING", False,
                       f"{size.detail} (binding: {size.binding_constraint})"),))
        value_aud = Decimal("0.00")

    now = clock.now()
    state = "pending_approval" if check.passed else "rejected"
    # Insert in 'risk_review' first: the pending_approval_requires_check
    # constraint (Doc 04 §2.1, structural) forbids awaiting approval before
    # the check row exists — one UPDATE lands state + check reference together.
    proposal_id = session.execute(text(
        "INSERT INTO trading.trade_proposals "
        "(instrument_id, market, action, committee_memo_id, signal_ids, entry_price, "
        " stop_loss, target_price, position_size, position_value_aud, state, "
        " expires_at, created_at) "
        "VALUES (:iid, :mkt, 'buy', :memo, :sids, :entry, :stop, :target, :qty, "
        "        :value, 'risk_review', :exp, :ca) RETURNING id"),
        {"iid": inst.id, "mkt": inst.market, "memo": UUID(memo_id),
         "sids": [UUID(s) for s in signal_refs], "entry": entry_price,
         "stop": stop_price, "target": target_price, "qty": qty,
         "value": value_aud, "exp": now + PROPOSAL_TTL, "ca": now}).scalar_one()

    check_id = _persist_check(
        session, clock, proposal_id=proposal_id, limits=limits, book=book,
        check=check, kind="proposal",
        price_snapshot={"entry_price": str(entry_price), "stop_price": str(stop_price),
                        "fx_to_aud": str(fx), "nav_aud": str(book.state.nav_aud)})
    session.execute(text(
        "UPDATE trading.trade_proposals SET state = :s, risk_check_id = :c "
        "WHERE id = :p"),
        {"s": state, "c": UUID(check_id) if check.passed else None,
         "p": proposal_id})

    _audit(session, clock).append(
        event_type="proposal.created", entity_type="proposal",
        entity_id=str(proposal_id), actor_type="dcp", actor_id="trading_lifecycle",
        payload={"proposal_id": str(proposal_id), "symbol": symbol, "action": "buy",
                 "memo_id": memo_id, "qty": qty, "state": state,
                 "expires_at": (now + PROPOSAL_TTL).isoformat()})

    return ProposalResult(
        proposal_id=str(proposal_id), state=state,
        verdict="PASS" if check.passed else "FAIL", risk_check_id=check_id,
        qty=qty, failures=tuple(r.rule for r in check.failures()))


# --------------------------------------------------------------- expiry (§5)

def expire_stale(session: Session, clock: Clock) -> tuple[str, ...]:
    """pending_approval past the 24h TTL -> 'expired' (+ audit per proposal)."""
    rows = session.execute(text(
        "UPDATE trading.trade_proposals SET state = 'expired' "
        "WHERE state = 'pending_approval' AND expires_at <= :now "
        "RETURNING id, expires_at"), {"now": clock.now()}).all()
    audit = _audit(session, clock)
    for r in rows:
        audit.append(event_type="proposal.expired", entity_type="proposal",
                     entity_id=str(r.id), actor_type="dcp",
                     actor_id="trading_lifecycle",
                     payload={"proposal_id": str(r.id),
                              "expires_at": r.expires_at.isoformat()})
    return tuple(str(r.id) for r in rows)


# ----------------------------------------------------- approve / reject (§3.2)

def _original_check(session: Session, check_id: UUID) -> RiskCheck:
    row = session.execute(text(
        "SELECT verdict, results, price_snapshot FROM risk.risk_checks "
        "WHERE id = :c"), {"c": check_id}).one()
    return RiskCheck(
        passed=row.verdict == "PASS",
        breaker=BreakerLevel(row.price_snapshot["breaker"]),
        results=tuple(RuleResult(d["rule"], bool(d["pass"]), str(d.get("detail", "")))
                      for d in row.results))


def _approve_exit(session: Session, clock: Clock, *, row: Any,
                  now: datetime) -> ApprovalOutcome:
    """Approval branch for action='exit' (discretionary close, Doc 04 §5).

    The fresh approval-time check does NOT run engine.validate: L1-L11 gate
    risk ADDED to the book and an exit only releases risk — under DD2/DD3 the
    breaker is exit-only, never exit-blocking, so the check records a breaker
    STATEMENT instead of a gate. What IS re-verified is the exit premise: the
    position must still be open at exactly the approved quantity (a stop exit
    may have closed it between proposal and click — that voids this approval
    with the same RISK_RECHECK_FAILED shape as the buy path). The breaker is
    folded from the persisted NAV history only: approving an exit must never
    depend on being able to mark every holding (fail-closed marks elsewhere
    in the book must not trap the principal in a position)."""
    pid = row.id
    proposal_id = str(pid)
    audit = _audit(session, clock)
    inst = _load_instrument_by_id(session, row.instrument_id)
    pos = session.execute(text(
        "SELECT id, qty FROM trading.positions "
        "WHERE instrument_id = :iid AND closed_at IS NULL AND qty > 0 FOR UPDATE"),
        {"iid": row.instrument_id}).first()
    breaker = _breaker_fold(_snapshot_navs(session), _confirmed_clearances(session))
    if pos is None:
        exit_ok, exit_detail = False, "position already closed — nothing to exit"
    elif int(pos.qty) != int(row.position_size):
        exit_ok = False
        exit_detail = (f"position qty {int(pos.qty)} != approved exit qty "
                       f"{int(row.position_size)} — stale exit proposal")
    else:
        exit_ok = True
        exit_detail = f"risk-reducing: closes {int(row.position_size)} {inst.symbol}"
    results: list[dict[str, Any]] = [
        {"rule": "DD", "pass": True, "value": None, "limit": None,
         "detail": f"breaker {breaker.value}: exits remain allowed "
                   "(DD3 is exit-only, not exit-blocking — Doc 04 §5)"},
        {"rule": "EXIT", "pass": exit_ok, "value": None, "limit": None,
         "detail": exit_detail}]
    fresh_id = _persist_static_check(
        session, clock, proposal_id=pid, kind="approval_time",
        verdict="PASS" if exit_ok else "FAIL", results=results,
        price_snapshot={"breaker": breaker.value,
                        # Doc 04 §14: the approval-time market price
                        "approval_price": str(
                            _latest_close(session, row.instrument_id, now.date()))})

    if not exit_ok:  # same terminal shape as the buy path's voided re-check
        session.execute(text(
            "UPDATE trading.trade_proposals SET state = 'voided' WHERE id = :p"),
            {"p": pid})
        audit.append(event_type="proposal.voided", entity_type="proposal",
                     entity_id=proposal_id, actor_type="dcp", actor_id="risk_engine",
                     payload={"proposal_id": proposal_id, "risk_check_id": fresh_id,
                              "failures": ["EXIT"]})
        return ApprovalOutcome(status="RISK_RECHECK_FAILED", proposal_id=proposal_id,
                               order_id=None, risk_check_id=fresh_id,
                               failures=("EXIT",))

    approval_id = session.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, auth_method, "
        " approval_time_risk_check_id, decided_at, created_at) "
        "VALUES (:p, 'approve', 'principal', 'console', :c, :t, :t) RETURNING id"),
        {"p": pid, "c": UUID(fresh_id), "t": now}).scalar_one()
    session.execute(text(
        "UPDATE trading.trade_proposals SET state = 'approved' WHERE id = :p"),
        {"p": pid})
    order_id = session.execute(text(
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, broker, "
        " side, qty, order_type, state, created_at) "
        "VALUES (:p, :a, :c, 'paper', 'sell', :q, 'market', 'pending_submit', :t) "
        "RETURNING id"),
        {"p": pid, "a": approval_id, "c": UUID(fresh_id),
         "q": int(row.position_size), "t": now}).scalar_one()
    audit.append(event_type="proposal.approved", entity_type="proposal",
                 entity_id=proposal_id, actor_type="human", actor_id="principal",
                 payload={"proposal_id": proposal_id, "approval_id": str(approval_id),
                          "order_id": str(order_id), "risk_check_id": fresh_id})
    audit.append(event_type="order.state_changed", entity_type="order",
                 entity_id=str(order_id), actor_type="dcp", actor_id="trading_lifecycle",
                 payload={"order_id": str(order_id), "from": None,
                          "to": "pending_submit"})
    return ApprovalOutcome(status="approved", proposal_id=proposal_id,
                           order_id=str(order_id), risk_check_id=fresh_id)


def approve(session: Session, clock: Clock, *, proposal_id: str,
            acknowledged_risks: bool) -> ApprovalOutcome:
    """Doc 06 §3.2 server sequence, minus HTTP: verify pending_approval and not
    expired -> RE-RUN the risk check on a fresh snapshot -> a now-FAIL voids
    the action (structured RISK_RECHECK_FAILED, never an exception) -> else
    record the approval, create the order in 'pending_submit', emit events."""
    if not acknowledged_risks:
        raise ValueError("Doc 06 §3.2: approval requires acknowledged_risks=true")
    _lifecycle_lock(session)
    pid = UUID(proposal_id)
    row = session.execute(text(
        "SELECT id, instrument_id, position_size, entry_price, stop_loss, state, "
        "       expires_at, risk_check_id, action, origin FROM trading.trade_proposals "
        "WHERE id = :p FOR UPDATE"), {"p": pid}).first()
    if row is None:
        raise ValueError(f"unknown proposal {proposal_id}")
    if row.state != "pending_approval":
        raise ValueError(f"proposal is {row.state!r}, not pending_approval — "
                         "risk FAIL and terminal states have no approval path")
    now = clock.now()
    audit = _audit(session, clock)
    if now >= row.expires_at:
        session.execute(text(
            "UPDATE trading.trade_proposals SET state = 'expired' WHERE id = :p"),
            {"p": pid})
        audit.append(event_type="proposal.expired", entity_type="proposal",
                     entity_id=proposal_id, actor_type="dcp",
                     actor_id="trading_lifecycle",
                     payload={"proposal_id": proposal_id,
                              "expires_at": row.expires_at.isoformat()})
        return ApprovalOutcome(status="PROPOSAL_EXPIRED", proposal_id=proposal_id,
                               order_id=None, risk_check_id=None)

    if row.action == "exit":  # exits skip buy-side validation (module docstring)
        return _approve_exit(session, clock, row=row, now=now)

    # fresh state, fresh prices, fresh limits — no grandfathering (Doc 04 §2.2)
    limits = load_active_limit_set(session, now.date())
    inst = _load_instrument_by_id(session, row.instrument_id)
    book = _build_book(session, clock)
    # ADR-0014: a core BUY (origin='core_allocation') is rebalanced, not stopped —
    # it carries NULL stop_loss and ZERO stop-out risk. Represent it to the re-check
    # exactly as build_core_proposals does: stop == entry => risk_aud = 0, so it
    # contributes nothing to L6/L7 while L1-L5/L11 weight rules still bind (SPY 55%
    # must still clear L2's core cap). The stopless treatment is gated on the
    # POSITIVE origin marker ALONE, never inferred from a missing stop: an agent
    # proposal (which the DB forbids from carrying a NULL stop, migration 0022)
    # that somehow reached here FAILS CLOSED loudly — a dropped stop is a bug or
    # a tamper, never read as zero risk.
    entry = Decimal(row.entry_price)
    if row.origin == "core_allocation":
        stop = entry
    elif row.stop_loss is None:
        raise RuntimeError(
            f"proposal {proposal_id} has origin {row.origin!r} and a NULL "
            "stop_loss — only a core_allocation proposal may be stopless; a "
            "missing stop on any other origin is never inferred to be zero risk "
            "(ADR-0014, invariant 2)")
    else:
        stop = Decimal(row.stop_loss)
    proposal = _fresh_proposal_inputs(
        session, clock, inst, qty=int(row.position_size),
        entry_price=entry, stop_price=stop, book=book)
    recheck = recheck_at_approval(
        proposal=proposal, state_now=book.state, limits=limits,
        breaker=book.breaker, original_check=_original_check(session, row.risk_check_id))
    fresh_id = _persist_check(
        session, clock, proposal_id=pid, limits=limits, book=book,
        check=recheck.fresh, kind="approval_time",
        price_snapshot={"entry_price": str(proposal.entry_price),
                        "stop_price": str(proposal.stop_price),
                        "fx_to_aud": str(proposal.fx_to_aud),
                        "nav_aud": str(book.state.nav_aud),
                        # Doc 04 §14: decision, APPROVAL, and fill prices per
                        # trade — this is the approval-time market price
                        "approval_price": str(
                            _latest_close(session, row.instrument_id, now.date()))})

    if recheck.voided:  # a now-FAIL voids the approval action — terminal
        session.execute(text(
            "UPDATE trading.trade_proposals SET state = 'voided' WHERE id = :p"),
            {"p": pid})
        failures = tuple(r.rule for r in recheck.fresh.failures())
        audit.append(event_type="proposal.voided", entity_type="proposal",
                     entity_id=proposal_id, actor_type="dcp", actor_id="risk_engine",
                     payload={"proposal_id": proposal_id, "risk_check_id": fresh_id,
                              "failures": list(failures)})
        return ApprovalOutcome(status="RISK_RECHECK_FAILED", proposal_id=proposal_id,
                               order_id=None, risk_check_id=fresh_id,
                               failures=failures)

    approval_id = session.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, auth_method, "
        " approval_time_risk_check_id, decided_at, created_at) "
        "VALUES (:p, 'approve', 'principal', 'console', :c, :t, :t) RETURNING id"),
        {"p": pid, "c": UUID(fresh_id), "t": now}).scalar_one()
    session.execute(text(
        "UPDATE trading.trade_proposals SET state = 'approved' WHERE id = :p"),
        {"p": pid})
    order_id = session.execute(text(
        "INSERT INTO trading.orders (proposal_id, approval_id, risk_check_id, broker, "
        " side, qty, order_type, state, created_at) "
        "VALUES (:p, :a, :c, 'paper', 'buy', :q, 'market', 'pending_submit', :t) "
        "RETURNING id"),
        {"p": pid, "a": approval_id, "c": UUID(fresh_id),
         "q": int(row.position_size), "t": now}).scalar_one()
    audit.append(event_type="proposal.approved", entity_type="proposal",
                 entity_id=proposal_id, actor_type="human", actor_id="principal",
                 payload={"proposal_id": proposal_id, "approval_id": str(approval_id),
                          "order_id": str(order_id), "risk_check_id": fresh_id})
    audit.append(event_type="order.state_changed", entity_type="order",
                 entity_id=str(order_id), actor_type="dcp", actor_id="trading_lifecycle",
                 payload={"order_id": str(order_id), "from": None,
                          "to": "pending_submit"})
    return ApprovalOutcome(status="approved", proposal_id=proposal_id,
                           order_id=str(order_id), risk_check_id=fresh_id)


def reject(session: Session, clock: Clock, *, proposal_id: str,
           reason: str) -> ApprovalOutcome:
    """Human reject (Doc 06 resource map): record the decision, transition,
    audit. The reason is audited (approvals has no reason column). A proposal
    past its TTL lands in 'expired', not 'rejected' — the states must not lie
    about why a proposal died — returned as a structured PROPOSAL_EXPIRED
    outcome (not an exception) so the transition COMMITS."""
    _lifecycle_lock(session)
    pid = UUID(proposal_id)
    row = session.execute(text(
        "SELECT state, risk_check_id, expires_at FROM trading.trade_proposals "
        "WHERE id = :p FOR UPDATE"), {"p": pid}).first()
    if row is None:
        raise ValueError(f"unknown proposal {proposal_id}")
    if row.state != "pending_approval":
        raise ValueError(f"proposal is {row.state!r}, not pending_approval")
    now = clock.now()
    if now >= row.expires_at:
        session.execute(text(
            "UPDATE trading.trade_proposals SET state = 'expired' WHERE id = :p"),
            {"p": pid})
        _audit(session, clock).append(
            event_type="proposal.expired", entity_type="proposal",
            entity_id=proposal_id, actor_type="dcp", actor_id="trading_lifecycle",
            payload={"proposal_id": proposal_id,
                     "expires_at": row.expires_at.isoformat()})
        return ApprovalOutcome(status="PROPOSAL_EXPIRED", proposal_id=proposal_id,
                               order_id=None, risk_check_id=None)
    session.execute(text(
        "INSERT INTO trading.approvals (proposal_id, decision, approver, auth_method, "
        " approval_time_risk_check_id, decided_at, created_at) "
        "VALUES (:p, 'reject', 'principal', 'console', :c, :t, :t)"),
        {"p": pid, "c": row.risk_check_id, "t": now})
    session.execute(text(
        "UPDATE trading.trade_proposals SET state = 'rejected' WHERE id = :p"),
        {"p": pid})
    _audit(session, clock).append(
        event_type="proposal.rejected", entity_type="proposal",
        entity_id=proposal_id, actor_type="human", actor_id="principal",
        payload={"proposal_id": proposal_id, "reason": reason})
    return ApprovalOutcome(status="rejected", proposal_id=proposal_id,
                           order_id=None, risk_check_id=str(row.risk_check_id))


def cancel_order(session: Session, clock: Clock, *, order_id: str,
                 reason: str) -> None:
    """Human escape hatch for a stuck 'pending_submit' order (e.g. its fill
    session's bar never materialised): order -> 'cancelled', its proposal ->
    'voided', both audited. Without this, _build_book reserves the order's
    pro-forma capital forever."""
    _lifecycle_lock(session)
    oid = UUID(order_id)
    row = session.execute(text(
        "SELECT id, state, proposal_id FROM trading.orders "
        "WHERE id = :o FOR UPDATE"), {"o": oid}).first()
    if row is None:
        raise ValueError(f"unknown order {order_id}")
    if row.state != "pending_submit":
        raise ValueError(f"order is {row.state!r}, not pending_submit")
    now = clock.now()
    session.execute(text(
        "UPDATE trading.orders SET state = 'cancelled', closed_at = :t "
        "WHERE id = :o AND state = 'pending_submit'"), {"t": now, "o": oid})
    session.execute(text(
        "UPDATE trading.trade_proposals SET state = 'voided' WHERE id = :p"),
        {"p": row.proposal_id})
    audit = _audit(session, clock)
    audit.append(event_type="order.state_changed", entity_type="order",
                 entity_id=order_id, actor_type="human", actor_id="principal",
                 payload={"order_id": order_id, "from": "pending_submit",
                          "to": "cancelled", "reason": reason})
    audit.append(event_type="proposal.voided", entity_type="proposal",
                 entity_id=str(row.proposal_id), actor_type="dcp",
                 actor_id="trading_lifecycle",
                 payload={"proposal_id": str(row.proposal_id),
                          "order_id": order_id, "reason": "order cancelled"})


# ------------------------------------------------------------- settlement (§5)

def settle_orders(session: Session, clock: Clock,
                  broker: Broker | None = None) -> tuple[FillReport, ...]:
    """Fill every 'pending_submit' order whose next-session bar now exists.
    Orders without one simply stay pending (the normal overnight state), so
    re-running is idempotent: a filled order is never pending again.

    Doc 04 §2 item 3: the Execution service VERIFIES the risk-check reference
    before submission. The exact rule: the referenced check must be a PASS,
    and either (check_kind='approval_time' AND proposal 'approved') — the
    human-approved buy/exit path — or (check_kind='order_time' AND proposal
    'executed') — a pre-authorized stop-exit order, whose proposal is the
    already-executed ENTRY proposal (atlas.dcp.trading.exits). Anything else
    is an integrity breach (only writable by tampering, the lifecycle cannot
    produce it) and raises instead of filling. Stop-exit orders are created
    and filled in one transaction, so one arriving here unfilled is itself
    unreachable through the lifecycle; if one exists anyway its lineage is
    genuine, and it fills as a plain sell at the next session open."""
    _lifecycle_lock(session)
    b: Broker = broker if broker is not None else PaperBroker()
    as_of = clock.now()
    rows = session.execute(text(
        "SELECT o.id, o.qty, o.side, o.created_at, tp.id AS proposal_id, "
        "       tp.state AS proposal_state, tp.entry_price, tp.stop_loss, "
        "       tp.origin, tp.committee_memo_id, i.id AS iid, i.symbol, i.market, "
        "       i.currency, rc.verdict AS check_verdict, rc.check_kind AS check_kind "
        "FROM trading.orders o "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "JOIN risk.risk_checks rc ON rc.id = o.risk_check_id "
        "WHERE o.state = 'pending_submit' ORDER BY o.created_at "
        "FOR UPDATE OF o")).all()
    reports: list[FillReport] = []
    for r in rows:
        lineage_ok = r.check_verdict == "PASS" and (
            (r.check_kind == "approval_time" and r.proposal_state == "approved")
            or (r.check_kind == "order_time" and r.proposal_state == "executed"))
        if not lineage_ok:
            raise RuntimeError(
                f"REFUSING to fill order {r.id}: proposal state "
                f"{r.proposal_state!r}, referenced check verdict "
                f"{r.check_verdict!r}/{r.check_kind!r} — an order must "
                "reference a PASS approval-time check on an approved proposal, "
                "or a PASS order-time check on an executed entry proposal "
                "(stop exit) (Doc 04 §2.3)")
        fill = b.submit(session, OrderTicket(
            order_id=str(r.id), instrument_id=r.iid, market=r.market,
            currency=r.currency, side=r.side, qty=int(r.qty),
            decision_price=Decimal(r.entry_price),
            decision_date=r.created_at.astimezone(UTC).date()), as_of=as_of)
        if fill is None:
            continue
        reports.append(_record_fill(session, clock, r, fill))
    return tuple(reports)


def _dispose_lots_fifo(session: Session, *, position_id: UUID, qty: int,
                       per_share_aud: Decimal, disposed_at: datetime,
                       now: datetime) -> Decimal:
    """FIFO tax-lot disposal (module docstring resolutions): oldest open lots
    first (acquired_at, then created_at, id). A partial disposal splits the
    lot — the original row becomes the DISPOSED slice (pro-rata cost,
    quantized to cents) and a new row keeps the residual qty with the exact
    residual cost, so no cent is created or destroyed. Returns the lot-booked
    proceeds total. Raises if the open lots cannot cover the disposal — a
    lot ledger that disagrees with the position is corruption, never rounded
    over (fail closed)."""
    lots = session.execute(text(
        "SELECT id, execution_id, qty, cost_aud, acquired_at FROM trading.tax_lots "
        "WHERE position_id = :p AND disposed_at IS NULL "
        "ORDER BY acquired_at, created_at, id FOR UPDATE"), {"p": position_id}).all()
    remaining = qty
    proceeds_total = Decimal("0.00")
    for lot in lots:
        if remaining == 0:
            break
        take = min(int(lot.qty), remaining)
        proceeds = (Decimal(take) * per_share_aud).quantize(_CENT)
        if take == int(lot.qty):
            session.execute(text(
                "UPDATE trading.tax_lots SET disposed_at = :at, proceeds_aud = :pr "
                "WHERE id = :i"), {"at": disposed_at, "pr": proceeds, "i": lot.id})
        else:
            disposed_cost = (Decimal(lot.cost_aud) * take / int(lot.qty)).quantize(_CENT)
            session.execute(text(  # original row becomes the disposed slice
                "UPDATE trading.tax_lots SET qty = :q, cost_aud = :c, "
                "disposed_at = :at, proceeds_aud = :pr WHERE id = :i"),
                {"q": take, "c": disposed_cost, "at": disposed_at, "pr": proceeds,
                 "i": lot.id})
            session.execute(text(  # residual keeps its acquisition identity
                "INSERT INTO trading.tax_lots (position_id, execution_id, qty, "
                " cost_aud, acquired_at, created_at) "
                "VALUES (:p, :e, :q, :c, :at, :ca)"),
                {"p": position_id, "e": lot.execution_id, "q": int(lot.qty) - take,
                 "c": Decimal(lot.cost_aud) - disposed_cost,
                 "at": lot.acquired_at, "ca": now})
        proceeds_total += proceeds
        remaining -= take
    if remaining != 0:
        raise RuntimeError(
            f"open tax lots cover only {qty - remaining} of a {qty}-share "
            f"disposal for position {position_id} — the lot ledger does not "
            "match the position; refusing to fill")
    return proceeds_total


def _record_fill(session: Session, clock: Clock, order: Any, fill: Fill) -> FillReport:
    """Execution + shortfall fields (Doc 04 §14), book mutation, order
    'filled', proposal 'executed' — all audited. Buys upsert the position at
    average cost and open a tax lot; sells reduce the position (avg_cost
    untouched — realised P&L lives in the lots), dispose lots FIFO, and close
    the position at the fill's executed_at when qty reaches zero. The
    UNIQUE(executions.order_id) index and the state-guarded order UPDATE are
    the schema backstops against a double fill."""
    now = clock.now()
    execution_id = session.execute(text(
        "INSERT INTO trading.executions (order_id, fill_qty, fill_price, fees, "
        " fx_rate_used, broker_exec_id, decision_price, shortfall_bps, executed_at, "
        " created_at) "
        "VALUES (:o, :q, :px, :fees, :fx, :bx, :dp, :sf, :at, :ca) RETURNING id"),
        {"o": order.id, "q": fill.fill_qty, "px": fill.fill_price, "fees": fill.fees,
         "fx": fill.fx_to_aud, "bx": f"paper-{order.id}", "dp": fill.decision_price,
         "sf": fill.shortfall_bps, "at": fill.executed_at, "ca": now}).scalar_one()
    moved = session.execute(text(
        "UPDATE trading.orders SET state = 'filled', submitted_at = :at, "
        "closed_at = :at WHERE id = :o AND state = 'pending_submit' RETURNING id"),
        {"at": fill.executed_at, "o": order.id}).all()
    if len(moved) != 1:
        raise RuntimeError(f"order {order.id} left 'pending_submit' mid-fill — "
                           "refusing to record a second fill")

    audit = _audit(session, clock)
    pos = session.execute(text(
        "SELECT id, qty, avg_cost, current_stop, is_core FROM trading.positions "
        "WHERE instrument_id = :iid AND closed_at IS NULL FOR UPDATE"),
        {"iid": order.iid}).first()
    if order.side == "sell":
        if pos is None:
            raise RuntimeError(f"sell order {order.id} has no open position in "
                               f"{order.symbol} — integrity breach, refusing to fill")
        if fill.fill_qty > int(pos.qty):
            raise RuntimeError(f"sell order {order.id} for {fill.fill_qty} exceeds "
                               f"open qty {int(pos.qty)} in {order.symbol} — "
                               "refusing to fill (long-only: no shorts, ever)")
        position_id = pos.id
        proceeds = _dispose_lots_fifo(
            session, position_id=pos.id, qty=fill.fill_qty,
            per_share_aud=fill.fill_price * fill.fx_to_aud,
            disposed_at=fill.executed_at, now=now)
        new_qty = int(pos.qty) - fill.fill_qty
        if new_qty == 0:
            session.execute(text(
                "UPDATE trading.positions SET qty = 0, closed_at = :at "
                "WHERE id = :i"), {"at": fill.executed_at, "i": pos.id})
            audit.append(event_type="position.closed", entity_type="position",
                         entity_id=str(pos.id), actor_type="dcp",
                         actor_id="trading_lifecycle",
                         payload={"position_id": str(pos.id),
                                  "symbol": order.symbol, "qty": fill.fill_qty,
                                  "proceeds_aud": str(proceeds)})
        else:  # reduce: avg_cost unchanged — realised P&L lives in the lots
            session.execute(text(
                "UPDATE trading.positions SET qty = :q WHERE id = :i"),
                {"q": new_qty, "i": pos.id})
            audit.append(event_type="position.reduced", entity_type="position",
                         entity_id=str(pos.id), actor_type="dcp",
                         actor_id="trading_lifecycle",
                         payload={"position_id": str(pos.id),
                                  "symbol": order.symbol, "qty": fill.fill_qty,
                                  "remaining_qty": new_qty,
                                  "proceeds_aud": str(proceeds)})
    elif pos is None:
        # ADR-0014: a settled origin='core_allocation' proposal opens a CORE
        # position (rebalanced, not stopped -> zero L7 open risk). Every other
        # origin ('agent') opens a satellite bound by the stop-based rule; a
        # dropped stop on a satellite must still fail closed. The marker is
        # POSITIVE and taken straight from the proposal origin, never inferred.
        is_core = order.origin == "core_allocation"
        position_id = session.execute(text(
            "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
            " opened_at, current_stop, thesis_memo_id, is_core, created_at) "
            "VALUES (:iid, :q, :px, :ccy, :at, :stop, :memo, :core, :ca) RETURNING id"),
            {"iid": order.iid, "q": fill.fill_qty, "px": fill.fill_price,
             "ccy": order.currency, "at": fill.executed_at, "stop": order.stop_loss,
             "memo": order.committee_memo_id, "core": is_core,
             "ca": now}).scalar_one()
        audit.append(event_type="position.opened", entity_type="position",
                     entity_id=str(position_id), actor_type="dcp",
                     actor_id="trading_lifecycle",
                     payload={"position_id": str(position_id),
                              "symbol": order.symbol, "qty": fill.fill_qty,
                              "stop": str(order.stop_loss)})
    else:
        # ADR-0014 safety (adversarial-review finding 2026-07-16): a position row
        # is EITHER core or satellite, never mixed. The partial unique index
        # (one open row per instrument) forces a same-instrument add through this
        # merge branch, so a cross-origin fold — an agent add into a core row, or
        # a core rebalance into a satellite row — would inherit the row's is_core
        # and silently zero (or wrongly count) the added shares' L7 stop risk.
        # Refuse it fail-closed; is_core is decided at open and never mutates here.
        order_is_core = order.origin == "core_allocation"
        if bool(pos.is_core) != order_is_core:
            raise RuntimeError(
                f"cross-origin merge refused for {order.symbol}: an order with "
                f"origin '{order.origin}' cannot fold into an is_core="
                f"{pos.is_core} position — a core holding and a satellite "
                "holding may not share one position row")
        new_qty = int(pos.qty) + fill.fill_qty
        new_avg = ((Decimal(pos.qty) * Decimal(pos.avg_cost)
                    + Decimal(fill.fill_qty) * fill.fill_price)
                   / Decimal(new_qty)).quantize(_QTY6)
        if order_is_core:
            new_stop = None            # core is rebalanced, not stopped (stays stopless)
        else:
            # tighten-only stop merge: a looser add-on stop must not expand the
            # open risk L7 approved for the existing quantity (long-only: higher
            # stop = less risk)
            old_stop = Decimal(pos.current_stop) if pos.current_stop is not None else None
            new_stop = (Decimal(order.stop_loss) if old_stop is None
                        else max(old_stop, Decimal(order.stop_loss)))
        session.execute(text(
            "UPDATE trading.positions SET qty = :q, avg_cost = :avg, "
            "current_stop = :stop WHERE id = :i"),
            {"q": new_qty, "avg": new_avg, "stop": new_stop, "i": pos.id})
        position_id = pos.id
        audit.append(event_type="position.increased", entity_type="position",
                     entity_id=str(position_id), actor_type="dcp",
                     actor_id="trading_lifecycle",
                     payload={"position_id": str(position_id),
                              "symbol": order.symbol, "qty": new_qty,
                              "added_qty": fill.fill_qty,
                              "stop_old": str(old_stop), "stop_new": str(new_stop)})
    if order.side != "sell":  # buys ACQUIRE a lot; sells disposed theirs above
        session.execute(text(
            "INSERT INTO trading.tax_lots (position_id, execution_id, qty, cost_aud, "
            " acquired_at, created_at) VALUES (:p, :e, :q, :c, :at, :ca)"),
            {"p": position_id, "e": execution_id, "q": fill.fill_qty,
             "c": (Decimal(fill.fill_qty) * fill.fill_price * fill.fx_to_aud
                   ).quantize(_CENT),
             "at": fill.executed_at, "ca": now})

    audit.append(event_type="execution.recorded", entity_type="execution",
                 entity_id=str(execution_id), actor_type="broker", actor_id="paper",
                 payload={"execution_id": str(execution_id), "order_id": str(order.id),
                          "fill_qty": fill.fill_qty, "fill_price": str(fill.fill_price),
                          "fill_date": fill.fill_date.isoformat(),
                          "decision_price": str(fill.decision_price),
                          "shortfall_bps": str(fill.shortfall_bps)})
    audit.append(event_type="order.state_changed", entity_type="order",
                 entity_id=str(order.id), actor_type="dcp", actor_id="trading_lifecycle",
                 payload={"order_id": str(order.id), "from": "pending_submit",
                          "to": "filled"})
    # A stop-exit fill references the ENTRY proposal, already 'executed' by the
    # entry fill — transition (and the event) only happen once per proposal.
    if order.proposal_state != "executed":
        session.execute(text(
            "UPDATE trading.trade_proposals SET state = 'executed' WHERE id = :p"),
            {"p": order.proposal_id})
        audit.append(event_type="proposal.executed", entity_type="proposal",
                     entity_id=str(order.proposal_id), actor_type="dcp",
                     actor_id="trading_lifecycle",
                     payload={"proposal_id": str(order.proposal_id),
                              "order_id": str(order.id)})
    return FillReport(order_id=str(order.id), execution_id=str(execution_id),
                      fill_date=fill.fill_date, fill_price=fill.fill_price,
                      shortfall_bps=fill.shortfall_bps)


# --------------------------------------------------------------- snapshot (§5)

def snapshot(session: Session, clock: Clock) -> SnapshotResult:
    """Mark the book: positions at latest vendor closes, FX-translated, through
    compute_snapshot (Doc 03) into a trading.portfolio_snapshots row."""
    now = clock.now()
    on = now.date()
    positions = session.execute(text(
        "SELECT p.qty, p.current_stop, p.is_core, i.id AS iid, i.symbol, i.currency "
        "FROM trading.positions p JOIN market.instruments i ON i.id = p.instrument_id "
        "WHERE p.closed_at IS NULL AND p.qty > 0")).all()

    rates: dict[str, Decimal] = {"AUD": Decimal(1)}
    marks: list[Holding] = []
    holdings_json: list[dict[str, Any]] = []
    open_risk = Decimal(0)
    for p in positions:
        rates.setdefault(p.currency, fx_to_aud(session, p.currency, on))
        close = _latest_close(session, p.iid, on)
        fx = rates[p.currency]
        value = (Decimal(p.qty) * close * fx).quantize(_CENT)
        # open_risk_pct mirrors the L7 gate (ADR-0014): a core position is
        # rebalanced, not stopped, so it contributes ZERO; a satellite with no
        # stop fails closed to its full value; a stopped satellite contributes
        # its stop-out loss. Reporting must not disagree with the gate.
        if p.is_core:
            open_risk += Decimal(0)
        elif p.current_stop is None:  # fail closed (no stop = fully at risk)
            open_risk += value
        else:
            open_risk += max(Decimal(0),
                             (close - Decimal(p.current_stop))) * Decimal(p.qty) * fx
        marks.append(Holding(symbol=p.symbol, qty=p.qty, currency=p.currency,
                             last_price=close))
        holdings_json.append({"symbol": p.symbol, "qty": int(p.qty),
                              "currency": p.currency, "last_price": str(close),
                              "value_aud": str(value)})

    snap = compute_snapshot(cash_aud=_ledger_cash(session), holdings=marks,
                            fx_to_aud=rates)
    open_risk_pct = (open_risk / snap.nav_aud).quantize(_PCT)
    # Doc 04 §5: breaker state changes are audit events. The new NAV point may
    # move the latched fold — compare before/after and record the transition.
    points_before = _snapshot_navs(session)
    clearances = _confirmed_clearances(session)
    breaker_before = _breaker_fold(points_before, clearances)
    breaker_after = _breaker_fold([*points_before, (now, snap.nav_aud)], clearances)
    snapshot_id = session.execute(text(
        "INSERT INTO trading.portfolio_snapshots (as_of, nav_aud, cash_aud, holdings, "
        " exposures, fx_rates, open_risk_pct) "
        "VALUES (:at, :nav, :cash, CAST(:h AS jsonb), CAST(:e AS jsonb), "
        "        CAST(:fx AS jsonb), :orp) RETURNING id"),
        {"at": now, "nav": snap.nav_aud, "cash": snap.cash_aud,
         "h": json.dumps(holdings_json),
         "e": json.dumps({"weights": {k: str(v) for k, v in snap.weights.items()},
                          "non_aud_pct": str(snap.non_aud_exposure_pct)}),
         "fx": json.dumps({k: str(v) for k, v in rates.items()}),
         "orp": open_risk_pct}).scalar_one()
    audit = _audit(session, clock)
    audit.append(
        event_type="portfolio.snapshot.created", entity_type="portfolio",
        entity_id=str(snapshot_id), actor_type="dcp", actor_id="trading_lifecycle",
        payload={"snapshot_id": str(snapshot_id), "nav_aud": str(snap.nav_aud),
                 "cash_aud": str(snap.cash_aud),
                 "open_risk_pct": str(open_risk_pct)})
    if breaker_after is not breaker_before:
        audit.append(
            event_type="drawdown.breaker.changed", entity_type="portfolio",
            entity_id=str(snapshot_id), actor_type="dcp", actor_id="risk_engine",
            payload={"from": breaker_before.value, "to": breaker_after.value,
                     "nav_aud": str(snap.nav_aud),
                     "snapshot_id": str(snapshot_id)})
    return SnapshotResult(snapshot_id=str(snapshot_id), nav_aud=snap.nav_aud,
                          cash_aud=snap.cash_aud, open_risk_pct=open_risk_pct)
