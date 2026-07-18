"""Passive index-core allocation (ADR-0012, signed 2026-07-15).

The core (default SPY 55% + INDA 15% of NAV) is NOT an agent recommendation.
It is a DETERMINISTIC, Principal-parameterised target-weight policy — "hold the
market" needs no committee, no signal, no thesis. This module computes the
integer-share rebalance to approach the target weights and persists the legs as
trade_proposals with origin='core_allocation' (migration 0022's carve-out),
routed through the SAME risk engine agent proposals use.

Two pieces:
  * plan_core_rebalance(...)  — a PURE function: NAV + targets + current book ->
    the integer-share buy/sell legs, acting only when a leg is outside the drift
    band (ADR-0012: +/-5pp default), whole shares only, never over-allocating,
    leaving a documented per-leg cash residual. No DB, no clock, no side effects.
  * build_core_proposals(...) — persists those legs as trade_proposals and runs
    each through the risk engine (imported, never reimplemented). BUY legs run
    the full L1-L11 validate(); SELL legs release exposure and take the Doc 04
    §5 exit treatment (buy-side limits n/a). PASS -> 'pending_approval' awaiting
    the Principal's seal; FAIL is terminal ('rejected') — invariant 3 holds even
    for the core (a core buy cannot bypass a risk FAIL).

The core is REBALANCED, NOT STOPPED (ADR-0012): every leg carries stop_loss
NULL. For the risk engine's stop-based rules (L6 trade risk, L7 aggregate open
risk) a no-stop core leg is passed with stop == entry, so its stop-out risk is
ZERO — the core consumes no stop-loss risk budget; its market exposure is
captured by the weight rules (L1/L2/L3/L4/L5/L11), which is the honest picture.

entry_price and target_price stay NOT NULL for every origin (migration 0022
relaxes ONLY the three evidence columns), so a core leg records the deterministic
reference close in BOTH: it is the mark the leg was sized on, and a passive hold
has no price target (documented placeholder, not a signal-derived target).

KNOWN LIMIT INTERACTION (reported, not silently worked around): under the active
limit set (limit_set_v1) L2 caps single-ETF weight at 15%, so a 55% SPY leg
fails L2 and lands 'rejected'. Deploying the full-size SPY core requires the
Principal-signed limit-set v2 (an index-core ETF class) that board-memo item 8
already prescribes via dual-confirm change control — out of scope here and NOT
weakened by this module. INDA at 15% clears under v1 today.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.execution.paper import fx_to_aud
from atlas.dcp.risk.engine import (
    HoldingRisk,
    Limits,
    PortfolioState,
    load_active_limit_set,
    validate,
)
from atlas.dcp.trading.proposals import (
    _Book,
    _Instrument,
    _audit,
    _build_book,
    _fresh_proposal_inputs,
    _latest_close,
    _lifecycle_lock,
    _load_instrument,
    _persist_check,
    _persist_static_check,
)

# ADR-0012 signed default allocation (weight-agnostic mechanics; a future signed
# amendment can change the numbers). Weights are FRACTIONS of NAV.
CORE_TARGETS: dict[str, Decimal] = {"SPY": Decimal("0.55"), "INDA": Decimal("0.15")}
DEFAULT_DRIFT_BAND_PP = Decimal("5")   # ADR-0012 rebalance trigger: +/-5 pp
_CENT = Decimal("0.01")

# CORE PROPOSAL TTL — 72h, deliberately LONGER than the agent 24h TTL
# (proposals.PROPOSAL_TTL, unchanged: an agent thesis is priced off a fresh
# signal and ages fast). Rationale (ops-reliability build, 2026-07): the core
# is a STANDING POLICY, not a timely signal — "hold the market" is exactly as
# true on Monday as it was on Friday, and the observed failure this fixes is
# that core proposals silently expired unapproved TWICE under the 24h TTL
# before the Principal ever saw them. A standing rebalance can wait for the
# weekend; 72h spans one. The re-check at approval (Doc 04 §2.2) still runs
# against fresh prices and fresh limits, so a longer queue life never means a
# stale risk verdict — staleness is re-priced at the click, not at creation.
CORE_PROPOSAL_TTL = timedelta(hours=72)

_THESIS = "Passive index core per ADR-0012 (deterministic target-weight; no agent)."


# ------------------------------------------------------------ pure rebalancer

@dataclass(frozen=True)
class CoreLeg:
    """One deterministic rebalance action toward a target weight. Amounts are in
    the instrument's LOCAL currency (ref_price) plus its AUD translation (fx)."""
    symbol: str
    action: Literal["buy", "sell"]
    qty: int                       # shares to trade (always positive)
    ref_price: Decimal             # reference close, local currency
    fx_to_aud: Decimal             # AUD per 1 unit of local currency
    target_weight: Decimal         # fraction of NAV, e.g. 0.55
    target_value_aud: Decimal      # target_weight * nav
    resulting_qty: int             # holding AFTER this leg fills
    resulting_value_aud: Decimal   # resulting_qty * ref_price * fx (<= target)
    cash_residual_aud: Decimal     # target_value - resulting_value (>= 0: undeployed)


def plan_core_rebalance(
    *,
    nav_aud: Decimal,
    targets: dict[str, Decimal],
    positions: dict[str, int],
    prices: dict[str, Decimal],
    fx: dict[str, Decimal],
    drift_band_pp: Decimal = DEFAULT_DRIFT_BAND_PP,
) -> list[CoreLeg]:
    """Integer-share rebalance toward `targets` (fractions of NAV).

    Deterministic and pure. For each target symbol (processed in sorted order):
      * current weight = current_qty * price * fx / nav;
      * if |current weight - target weight| <= drift_band -> NO leg (idempotent
        within the band, ADR-0012);
      * else buy/sell to the desired whole-share holding
        desired = floor(target_value / (price * fx)) — whole shares only, so the
        resulting value never exceeds target (never over-allocate); the leftover
        target_value - resulting_value is the documented per-leg cash residual.
    A symbol already at the desired whole-share holding yields no leg.
    """
    if nav_aud <= 0:
        raise ValueError("nav_aud must be positive")
    band = drift_band_pp / Decimal(100)
    legs: list[CoreLeg] = []
    for symbol in sorted(targets):
        target_w = targets[symbol]
        price = prices[symbol]
        rate = fx[symbol]
        if price <= 0 or rate <= 0:
            raise ValueError(f"price and fx must be positive for {symbol!r}")
        cur_qty = positions.get(symbol, 0)
        price_aud = price * rate
        cur_weight = (Decimal(cur_qty) * price_aud) / nav_aud
        if abs(cur_weight - target_w) <= band:
            continue                              # within drift band: hold
        target_value = nav_aud * target_w
        desired_qty = int((target_value / price_aud).to_integral_value(
            rounding=ROUND_FLOOR))
        delta = desired_qty - cur_qty
        if delta == 0:                            # already at the whole-share target
            continue
        resulting_value = Decimal(desired_qty) * price_aud
        legs.append(CoreLeg(
            symbol=symbol,
            action="buy" if delta > 0 else "sell",
            qty=abs(delta), ref_price=price, fx_to_aud=rate,
            target_weight=target_w, target_value_aud=target_value,
            resulting_qty=desired_qty, resulting_value_aud=resulting_value,
            cash_residual_aud=target_value - resulting_value))
    return legs


# ------------------------------------------------------------ persistence path

@dataclass(frozen=True)
class CoreProposalResult:
    proposal_id: str
    symbol: str
    action: str                    # 'buy' | 'sell'
    qty: int
    state: str                     # 'pending_approval' | 'rejected'
    verdict: str                   # 'PASS' | 'FAIL'
    risk_check_id: str | None      # set only when the check PASSED (Doc 04 §2.1)
    failures: tuple[str, ...]      # failing rule names, empty on PASS


def _core_positions(session: Session, ids: dict[str, UUID]) -> dict[str, int]:
    """Current open share count per core symbol (0 when unheld)."""
    out: dict[str, int] = {}
    for symbol, iid in ids.items():
        qty = session.execute(text(
            "SELECT qty FROM trading.positions "
            "WHERE instrument_id = :i AND closed_at IS NULL"), {"i": iid}).scalar()
        out[symbol] = int(qty) if qty is not None else 0
    return out


def _thread(state: PortfolioState, leg: CoreLeg, inst: _Instrument) -> PortfolioState:
    """Fold one ACCEPTED leg into the worst-case pro-forma so the next leg is
    checked against it (batched-rebalance honesty: two core buys are two new
    positions and their costs both draw down the L5 cash floor). Buys add a
    zero-stop-risk holding and reserve cash; sells release cash and trim the
    matching holding (conservatively, never below zero). Core siblings are NOT
    fed into each other's L8 correlation — their co-movement is the deliberate
    market exposure, not a satellite concentration risk."""
    trade_value = Decimal(leg.qty) * leg.ref_price * leg.fx_to_aud
    holdings = list(state.holdings)
    if leg.action == "buy":
        # ADR-0014: a core holding is marked is_core (the POSITIVE marker) and
        # carries no stop (risk_to_stop_aud None) -> the engine's L7 zeroes it.
        holdings.append(HoldingRisk(
            symbol=leg.symbol, value_aud=trade_value,
            sector_gics=inst.sector_gics, india_exposed=inst.india_exposed,
            currency=inst.currency, risk_to_stop_aud=None, is_core=True))
        return replace(state, cash_aud=state.cash_aud - trade_value,
                       holdings=tuple(holdings),
                       new_positions_today=state.new_positions_today + 1)
    for idx, h in enumerate(holdings):
        if h.symbol == leg.symbol:  # trim the sibling; is_core is preserved
            holdings[idx] = replace(
                h, value_aud=max(Decimal(0), h.value_aud - trade_value),
                risk_to_stop_aud=None)
            break
    return replace(state, cash_aud=state.cash_aud + trade_value,
                   holdings=tuple(holdings))


def _persist_core_leg(session: Session, clock: Clock, leg: CoreLeg, *,
                      inst: _Instrument, limits: Limits, book: _Book,
                      state: PortfolioState) -> tuple[CoreProposalResult, bool]:
    """Persist one core leg and risk-check it via the SAME path agent proposals
    use. Returns (result, accepted) — accepted legs thread into the next leg's
    pro-forma."""
    ref = leg.ref_price
    price_snapshot: dict[str, Any] = {
        "entry_price": str(ref), "fx_to_aud": str(leg.fx_to_aud),
        "nav_aud": str(state.nav_aud), "origin": "core_allocation",
        "core_no_stop": True}

    if leg.action == "buy":
        # stop == entry => the core leg carries ZERO stop-out risk (it is not
        # stopped); L1-L11 still run on the threaded worst-case book.
        proposal = _fresh_proposal_inputs(
            session, clock, inst, qty=leg.qty, entry_price=ref, stop_price=ref,
            book=book)
        check = validate(proposal, state, limits, book.breaker)
        passed = check.passed
        failures = tuple(r.rule for r in check.failures())
    else:
        # A sell releases exposure (Doc 04 §5): buy-side L1-L11 do not gate it.
        check = None
        passed, failures = True, ()

    now = clock.now()
    proposal_state = "pending_approval" if passed else "rejected"
    value_aud = (Decimal(leg.qty) * ref * leg.fx_to_aud).quantize(_CENT)
    # Insert 'risk_review' first: pending_approval_requires_check (Doc 04 §2.1)
    # forbids awaiting approval before the check row exists.
    proposal_id = session.execute(text(
        "INSERT INTO trading.trade_proposals "
        "(instrument_id, market, action, origin, committee_memo_id, signal_ids, "
        " entry_price, stop_loss, target_price, position_size, position_value_aud, "
        " state, thesis_summary, expires_at, created_at) "
        "VALUES (:iid, :mkt, :act, 'core_allocation', NULL, '{}', :entry, NULL, "
        "        :target, :qty, :value, 'risk_review', :thesis, :exp, :ca) "
        "RETURNING id"),
        {"iid": inst.id, "mkt": inst.market, "act": leg.action, "entry": ref,
         "target": ref, "qty": leg.qty, "value": value_aud, "thesis": _THESIS,
         "exp": now + CORE_PROPOSAL_TTL, "ca": now}).scalar_one()

    if check is not None:
        check_id = _persist_check(
            session, clock, proposal_id=proposal_id, limits=limits, book=book,
            check=check, kind="proposal", price_snapshot=price_snapshot)
    else:
        check_id = _persist_static_check(
            session, clock, proposal_id=proposal_id, kind="proposal",
            verdict="PASS", price_snapshot=price_snapshot,
            results=[{"rule": "CORE_SELL", "pass": True, "value": None,
                      "limit": None, "detail": "core rebalance sell releases "
                      "exposure; buy-side L1-L11 n/a (Doc 04 §5)"}])

    session.execute(text(
        "UPDATE trading.trade_proposals SET state = :s, risk_check_id = :c "
        "WHERE id = :p"),
        {"s": proposal_state, "c": UUID(check_id) if passed else None,
         "p": proposal_id})

    _audit(session, clock).append(
        event_type="proposal.created", entity_type="proposal",
        entity_id=str(proposal_id), actor_type="dcp", actor_id="core_allocation",
        payload={"proposal_id": str(proposal_id), "symbol": leg.symbol,
                 "action": leg.action, "origin": "core_allocation",
                 "qty": leg.qty, "state": proposal_state, "adr": "ADR-0012",
                 "ttl_hours": int(CORE_PROPOSAL_TTL.total_seconds() // 3600),
                 "expires_at": (now + CORE_PROPOSAL_TTL).isoformat()})

    return (CoreProposalResult(
        proposal_id=str(proposal_id), symbol=leg.symbol, action=leg.action,
        qty=leg.qty, state=proposal_state,
        verdict="PASS" if passed else "FAIL",
        risk_check_id=check_id if passed else None, failures=failures), passed)


def build_core_proposals(
    session: Session, clock: Clock, *,
    targets: dict[str, Decimal] = CORE_TARGETS,
    drift_band_pp: Decimal = DEFAULT_DRIFT_BAND_PP,
) -> list[CoreProposalResult]:
    """Compute the core rebalance against the live book and persist each leg as a
    trade_proposal (origin='core_allocation', no memo, empty signals, no stop),
    risk-checked through the imported engine. Idempotent by construction: a book
    already within the drift band yields zero legs and zero proposals.
    """
    _lifecycle_lock(session)
    on: date = clock.now().date()
    limits = load_active_limit_set(session, on)
    book = _build_book(session, clock)

    insts = {sym: _load_instrument(session, sym) for sym in targets}
    prices = {sym: _latest_close(session, insts[sym].id, on) for sym in targets}
    fx = {sym: fx_to_aud(session, insts[sym].currency, on) for sym in targets}
    positions = _core_positions(session, {sym: insts[sym].id for sym in targets})

    plan = plan_core_rebalance(
        nav_aud=book.state.nav_aud, targets=targets, positions=positions,
        prices=prices, fx=fx, drift_band_pp=drift_band_pp)

    results: list[CoreProposalResult] = []
    state = book.state
    for leg in plan:
        result, accepted = _persist_core_leg(
            session, clock, leg, inst=insts[leg.symbol], limits=limits,
            book=book, state=state)
        results.append(result)
        if accepted:
            state = _thread(state, leg, insts[leg.symbol])
    return results


# ------------------------------------------------- standing-core maintenance
#
# The core is a STANDING POLICY (ADR-0012): the drift band is either breached
# or it is not, every single day, independent of any signal. The observed
# operational failure this section fixes (ops-reliability build, 2026-07):
# core proposals were generated once, sat unapproved past their TTL, expired
# silently — twice — and NOTHING regenerated them, so the book stayed outside
# its signed band with no proposal in the queue. maintain_core_proposals is
# the daily invariant restorer: run from the cycle (t8c, fail-soft), it
# guarantees the core is always ONE CLICK away each morning — a live proposal
# per drifted leg, or a fresh one. It never duplicates (a live proposal is a
# no-op), never resurrects history (expired/rejected rows stay exactly as
# they died), and regenerates ONLY through build_core_proposals above — same
# risk engine, same audit trail, invariant 3 intact.

MAINTENANCE_EVENT = "core.maintenance.regenerated"


@dataclass(frozen=True)
class CoreMaintenanceReport:
    """One maintenance pass, per target symbol: absent from the universe,
    inside the band, drifted-but-covered (live proposal standing), or
    regenerated this run."""
    session_date: date
    missing: tuple[str, ...]                     # not in the active universe
    in_band: tuple[str, ...]                     # no drift: nothing to do
    live: tuple[str, ...]                        # drifted, live proposal exists
    regenerated: tuple[CoreProposalResult, ...]  # drifted, no live -> rebuilt

    def summary(self) -> str:
        parts: list[str] = []
        if self.regenerated:
            legs = ", ".join(f"{r.symbol}:{r.action} {r.qty} -> {r.state}"
                             for r in self.regenerated)
            parts.append(f"regenerated {legs}")
        if self.live:
            parts.append("live " + ", ".join(self.live))
        if self.in_band:
            parts.append("in band " + ", ".join(self.in_band))
        if self.missing:
            parts.append("not in universe " + ", ".join(self.missing))
        if not parts:
            return "core idle (no targets)"
        return "core " + " · ".join(parts)


def _active_symbol_exists(session: Session, symbol: str) -> bool:
    return session.execute(text(
        "SELECT 1 FROM market.instruments WHERE symbol = :s AND is_active "
        "LIMIT 1"), {"s": symbol}).first() is not None


def _live_core_proposal(session: Session, now: datetime, symbol: str) -> str | None:
    """A LIVE core proposal for `symbol`: pending_approval and not yet expired
    (awaiting the Principal's click), or approved (its order is in flight —
    the position just hasn't landed yet). Expired/rejected/voided rows are
    HISTORY, never live — they must not suppress regeneration."""
    row = session.execute(text(
        "SELECT tp.id FROM trading.trade_proposals tp "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE i.symbol = :s AND tp.origin = 'core_allocation' "
        "  AND (tp.state = 'approved' "
        "       OR (tp.state = 'pending_approval' AND tp.expires_at > :now)) "
        "ORDER BY tp.created_at DESC LIMIT 1"), {"s": symbol, "now": now}).first()
    return str(row.id) if row is not None else None


def maintain_core_proposals(
    session: Session, clock: Clock, *,
    targets: dict[str, Decimal] = CORE_TARGETS,
    drift_band_pp: Decimal = DEFAULT_DRIFT_BAND_PP,
) -> CoreMaintenanceReport:
    """Keep the standing core one click away (section comment above).

    Per target symbol, in sorted order:
      * not an active instrument      -> reported 'missing' (a fixture/dev DB
        without SPY/INDA idles honestly; in production this line on the brief
        IS the alarm that the core universe broke);
      * inside the drift band         -> no-op ('in_band');
      * drifted, live proposal exists -> no-op ('live') — never a duplicate;
      * drifted, none live            -> (re)generate via build_core_proposals
        (same risk-checked path, 72h TTL), audited as MAINTENANCE_EVENT.
    Deterministic and idempotent: a second run in the same state is all
    live/in_band and writes nothing.
    """
    _lifecycle_lock(session)
    now = clock.now()
    on: date = now.date()
    missing = tuple(sym for sym in sorted(targets)
                    if not _active_symbol_exists(session, sym))
    present = {sym: w for sym, w in targets.items() if sym not in missing}
    if not present:
        return CoreMaintenanceReport(session_date=on, missing=missing,
                                     in_band=(), live=(), regenerated=())

    book = _build_book(session, clock)
    insts = {sym: _load_instrument(session, sym) for sym in present}
    prices = {sym: _latest_close(session, insts[sym].id, on) for sym in present}
    fx = {sym: fx_to_aud(session, insts[sym].currency, on) for sym in present}
    positions = _core_positions(session, {sym: insts[sym].id for sym in present})
    plan = plan_core_rebalance(
        nav_aud=book.state.nav_aud, targets=present, positions=positions,
        prices=prices, fx=fx, drift_band_pp=drift_band_pp)

    drifted = {leg.symbol for leg in plan}
    in_band = tuple(sym for sym in sorted(present) if sym not in drifted)
    live: list[str] = []
    needed: dict[str, Decimal] = {}
    for sym in sorted(drifted):
        if _live_core_proposal(session, now, sym) is not None:
            live.append(sym)
        else:
            needed[sym] = present[sym]

    regenerated: tuple[CoreProposalResult, ...] = ()
    if needed:
        # the EXISTING build path: restricting targets to the uncovered legs
        # plans identical per-symbol legs (plan_core_rebalance treats each
        # symbol independently) through the same risk engine and audit trail.
        regenerated = tuple(build_core_proposals(
            session, clock, targets=needed, drift_band_pp=drift_band_pp))
        _audit(session, clock).append(
            event_type=MAINTENANCE_EVENT, entity_type="core_allocation",
            entity_id=on.isoformat(), actor_type="dcp",
            actor_id="core_maintenance",
            payload={"session_date": on.isoformat(), "adr": "ADR-0012",
                     "ttl_hours": int(CORE_PROPOSAL_TTL.total_seconds() // 3600),
                     "regenerated": [
                         {"proposal_id": r.proposal_id, "symbol": r.symbol,
                          "action": r.action, "qty": r.qty, "state": r.state,
                          "verdict": r.verdict} for r in regenerated],
                     "live": live, "in_band": list(in_band),
                     "missing": list(missing)})

    return CoreMaintenanceReport(session_date=on, missing=missing,
                                 in_band=in_band, live=tuple(live),
                                 regenerated=regenerated)
