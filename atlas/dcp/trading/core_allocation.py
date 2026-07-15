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
from datetime import date
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
    PROPOSAL_TTL,
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
         "exp": now + PROPOSAL_TTL, "ca": now}).scalar_one()

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
                 "qty": leg.qty, "state": proposal_state,
                 "adr": "ADR-0012", "expires_at": (now + PROPOSAL_TTL).isoformat()})

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
