"""Monthly attribution (Doc 04 §14, Doc 06 §2 GET /portfolio/attribution/{period}).

Pure READ-ONLY Decimal math over committed tables — no writes, no clock: the
requested (year, month) is the only time input, and every timestamp column
consulted (executions.executed_at, tax_lots.disposed_at, portfolio_snapshots
.as_of, agent_runs.created_at) is timestamptz, compared against half-open UTC
month bounds [first instant of the month, first instant of the next month).

Doc 04 §14: "Decision, approval, and fill prices recorded per trade; realised
shortfall recalibrates the backtester's cost model (Tier 1) and is a standing
line in monthly attribution." This module computes that standing line, plus
the realised/unrealised split and the LLM cost drag the Performance page
reports (Doc 06 'Performance (attribution, cost drag incl. LLM spend)').

Documented resolutions (ambiguities resolved and pinned by tests):
- ENTRY vs EXIT shortfall are SEPARATE lines keyed off the order side, never
  merged: a buy's decision_price is the proposal entry price (what the desk
  decided to pay), while a sell's decision_price is the AUTHORIZED STOP for a
  stop exit or the exit proposal's recorded close for a discretionary exit
  (atlas.dcp.trading: exits.py / proposals.py). Averaging bps across the two
  would average against two different kinds of anchor.
- Per-fill AUD shortfall cost = shortfall_bps / 10000 * decision_price *
  fill_qty * fx_rate_used. The sign carries straight through from the stored,
  audited shortfall_bps (paper.shortfall_bps: positive = worse than decision
  on EITHER side — buys filled higher, sells filled lower), so positive
  cost_aud = money lost to implementation. Fills are summed exactly and the
  SUM is quantized to cents; the stored 4dp shortfall_bps is authoritative
  (it may differ from the raw price delta by its own quantization, well below
  a cent at this book size).
- avg_bps is the fill-qty-weighted mean of shortfall_bps, quantized to 4dp;
  None when the side had no fills (a zero would fake a measurement).
- Realised P&L = SUM(proceeds_aud - cost_aud) over tax lots DISPOSED in the
  period (both columns already cent-quantized at booking). Lot SPLITS are
  safe: _dispose_lots_fifo leaves the residual row with disposed_at NULL, so
  only the disposed slice ever counts, in the month it was disposed.
- NAV boundary convention (unrealised swing): nav_start is the LATEST
  snapshot strictly BEFORE the period (the book as it entered the month),
  falling back to the EARLIEST snapshot inside the period when the series
  begins mid-month; nav_end is the LATEST snapshot INSIDE the period (a
  later month's snapshot never leaks back). Unless both resolve to two
  DISTINCT snapshot rows — "at least 2 snapshots in/adjacent to the period"
  — nav_start_aud, nav_end_aud and unrealised_swing_aud are ALL None: a
  single observation cannot measure a swing. Honest label: the swing is the
  book's total mark-to-market NAV move between those two snapshots; realised
  legs already settled into cash are inside it, so it is NOT additive with
  realised_pnl_aud.
- llm_spend_usd stays in USD at the column's own 4dp precision (Doc 05
  research.agent_runs.cost_usd) — a cost-accounting line, never
  FX-translated into the AUD ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Sequence

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.strategy_lifecycle import (
    classify,
    is_authoritative,
    is_research_shadow,
)

logger = logging.getLogger(__name__)

# Structured reason codes for an authoritative-book integrity breach (ADR-0018).
NON_AUTHORITATIVE_STRATEGY = "NON_AUTHORITATIVE_STRATEGY"
UNKNOWN_STRATEGY_STATE = "UNKNOWN_STRATEGY_STATE"
MISSING_STRATEGY_LINEAGE = "MISSING_STRATEGY_LINEAGE"
UNRESOLVED_SIGNAL = "UNRESOLVED_SIGNAL"
UNRESOLVED_PROPOSAL = "UNRESOLVED_PROPOSAL"
NON_AUTHORITATIVE_CLOSED_LOT = "NON_AUTHORITATIVE_CLOSED_LOT"
NON_AUTHORITATIVE_FILL = "NON_AUTHORITATIVE_FILL"
INVALID_AUTHORITATIVE_SNAPSHOT = "INVALID_AUTHORITATIVE_SNAPSHOT"


class PortfolioIntegrityError(RuntimeError):
    """Fail-closed authoritative-book integrity breach (ADR-0018). Carries a
    structured `reason_code` (one of the codes above) and safe `identifiers`
    (ids/families/states — never PII or account data) for logging and audit."""

    def __init__(self, reason_code: str, message: str, **identifiers: object) -> None:
        self.reason_code = reason_code
        self.identifiers = identifiers
        super().__init__(f"[{reason_code}] {message} — {identifiers}")


# Backward-compatible alias: a non-authoritative strategy is one integrity breach.
NonAuthoritativeBookError = PortfolioIntegrityError


def _breach(reason_code: str, message: str, **identifiers: object) -> "PortfolioIntegrityError":
    """Emit a STRUCTURED log line and return the fail-closed exception (req 13)."""
    logger.error("authoritative-book integrity breach reason=%s %s ids=%s",
                 reason_code, message, identifiers)
    return PortfolioIntegrityError(reason_code, message, **identifiers)


def _state_breach(state: str | None, *, disposed: bool, kind: str,
                  **ids: object) -> "PortfolioIntegrityError | None":
    """None if `state` is authoritative; otherwise the fail-closed breach with the
    right reason code. Classification is the canonical strategy_lifecycle."""
    if is_authoritative(state):
        return None
    if state is None:
        return _breach(MISSING_STRATEGY_LINEAGE,
                       f"{kind} resolves to a signal with no strategy", **ids)
    if is_research_shadow(state):
        code = (NON_AUTHORITATIVE_CLOSED_LOT if kind == "lot" and disposed
                else NON_AUTHORITATIVE_FILL if kind == "fill"
                else NON_AUTHORITATIVE_STRATEGY)
        return _breach(code, f"{kind} attributable to a non-authoritative "
                       f"(research_shadow) strategy [{state}]", state=state, **ids)
    # a known-but-non-authoritative or unmapped lifecycle state (suspended /
    # draft / validated / ... ) — never valid for authoritative reporting
    return _breach(UNKNOWN_STRATEGY_STATE,
                   f"{kind} attributable to a strategy in a non-authoritative "
                   f"state [{state}] (classify={classify(state)})",
                   state=state, **ids)


# The lot/fill -> strategy lineage, resolved with LEFT JOINs so a broken link
# surfaces as a NULL row (never silently dropped by an inner join). The LATERAL
# aggregates every DISTINCT strategy state the proposal's REAL signals resolve
# to; a lot/fill with no real signal (discretionary uuid5 or a core lot) yields
# n_strats=0 and makes no non-authoritative claim.
_LINEAGE_LATERAL = (
    "LEFT JOIN LATERAL (SELECT array_agg(DISTINCT st.state) AS states, "
    " count(DISTINCT st.id) AS n_strats FROM quant.signals sig "
    " JOIN quant.strategies st ON st.id = sig.strategy_id "
    " WHERE tp.signal_ids IS NOT NULL AND sig.id = ANY(tp.signal_ids)) lin ON true")


def assert_authoritative_book(session: Session) -> None:
    """Fail-closed integrity guard for an AUTHORITATIVE reporting period (ADR-0018):
    NO economically-contributing record — open OR disposed tax lot, or fill —
    may be attributable to a non-authoritative strategy, and no required lineage
    link (execution -> order -> proposal -> signal -> strategy) may be missing,
    unresolved, or mapped to an unknown/non-authoritative lifecycle state. Raises
    PortfolioIntegrityError with a structured reason code on the FIRST offender.

    Lineage is resolved with LEFT JOINs so a broken record surfaces (never
    silently dropped). is_core (passive) lots are authoritative by construction;
    a lot/fill that resolves to NO strategy (discretionary) makes no
    non-authoritative claim and is authoritative. research_shadow deploys no
    capital (the bridge guard), so a valid paper book passes unchanged."""
    # (1) every non-core tax lot — OPEN and DISPOSED (NAV + realised P&L). The
    # LEFT JOINs keep a lot even if its lineage is incomplete (never dropped by
    # an inner join); the LATERAL resolves the DISTINCT strategy states it maps to.
    lots = session.execute(text(
        "SELECT tl.id AS lot_id, (tl.disposed_at IS NOT NULL) AS disposed, "
        "       lin.states "
        "FROM trading.tax_lots tl "
        "LEFT JOIN trading.positions pos ON pos.id = tl.position_id "
        "LEFT JOIN trading.executions e ON e.id = tl.execution_id "
        "LEFT JOIN trading.orders o ON o.id = e.order_id "
        "LEFT JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        + _LINEAGE_LATERAL +
        " WHERE pos.is_core IS NOT TRUE")).mappings().all()
    for r in lots:
        # A lot that resolves to NO strategy (n_strats=0) makes no
        # non-authoritative claim — a manual/in-kind or discretionary holding,
        # authoritative by default (a real satellite lot ALWAYS carries a signal
        # whose strategy resolves via FK, so this is never shadow capital).
        for st in (r["states"] or []):    # each DISTINCT resolved strategy state
            breach = _state_breach(st, disposed=r["disposed"], kind="lot",
                                   lot_id=str(r["lot_id"]))
            if breach is not None:
                raise breach
    # (2) every fill/execution (implementation shortfall) — a shadow fill fails
    #     even after its lot is fully disposed. The LEFT JOIN keeps a fill whose
    #     lineage is broken (never dropped by an inner join); a fill that resolves
    #     to no strategy is discretionary, one that resolves to a shadow strategy
    #     fails closed.
    fills = session.execute(text(
        "SELECT e.id AS exec_id, lin.states "
        "FROM trading.executions e "
        "LEFT JOIN trading.orders o ON o.id = e.order_id "
        "LEFT JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        + _LINEAGE_LATERAL)).mappings().all()
    for r in fills:
        for st in (r["states"] or []):
            breach = _state_breach(st, disposed=False, kind="fill",
                                   execution_id=str(r["exec_id"]))
            if breach is not None:
                raise breach

_CENT = Decimal("0.01")
_BPS = Decimal("0.0001")
_USD4 = Decimal("0.0001")   # research.agent_runs.cost_usd is numeric(10,4)
_TEN_K = Decimal(10_000)


@dataclass(frozen=True)
class ShortfallLine:
    """One Doc 04 §14 shortfall line (entry or exit) over the period's fills
    on one order side. Positive cost_aud = implementation cost (fills worse
    than their decision prices); negative = the market paid the book."""
    fills: int
    qty: int
    avg_bps: Decimal | None     # fill-qty-weighted, 4dp; None when fills == 0
    cost_aud: Decimal           # signed sum, cents


@dataclass(frozen=True)
class Attribution:
    """One month of attribution (Doc 04 §14 standing line + Doc 06
    Performance page inputs). All Decimals quantized: cents for AUD, 4dp for
    bps and USD."""
    period: str                 # 'YYYY-MM'
    trades_buy: int             # fills recorded in the period, by order side
    trades_sell: int
    entry_shortfall: ShortfallLine   # buy fills vs proposal entry price
    exit_shortfall: ShortfallLine    # sell fills vs authorized stop / exit close
    realised_pnl_aud: Decimal   # lots disposed in the period: proceeds - cost
    lots_closed: int
    nav_start_aud: Decimal | None    # boundary snapshots (module docstring);
    nav_end_aud: Decimal | None      # all three None unless two distinct
    unrealised_swing_aud: Decimal | None  # snapshots resolve
    llm_spend_usd: Decimal      # research.agent_runs.cost_usd in the period


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Half-open UTC bounds [start, end) for the calendar month. Raises
    ValueError (via datetime) on impossible input — the API maps that to the
    Doc 06 §3.3 envelope, it never guesses a period."""
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1..12, got {month}")
    start = datetime(year, month, 1, tzinfo=UTC)
    end = (datetime(year + 1, 1, 1, tzinfo=UTC) if month == 12
           else datetime(year, month + 1, 1, tzinfo=UTC))
    return start, end


def _shortfall_line(fills: Sequence[Any]) -> ShortfallLine:
    """Fold one side's fills. Exact Decimal accumulation; the AUD sum is
    quantized once at the end (cents), the weighted bps mean to 4dp."""
    qty = sum(int(f.fill_qty) for f in fills)
    cost = Decimal(0)
    bps_weighted = Decimal(0)
    for f in fills:
        bps = Decimal(f.shortfall_bps)
        cost += (bps / _TEN_K) * Decimal(f.decision_price) \
            * Decimal(f.fill_qty) * Decimal(f.fx_rate_used)
        bps_weighted += bps * Decimal(f.fill_qty)
    avg = (bps_weighted / qty).quantize(_BPS, rounding=ROUND_HALF_EVEN) if qty else None
    return ShortfallLine(fills=len(fills), qty=qty, avg_bps=avg,
                         cost_aud=cost.quantize(_CENT, rounding=ROUND_HALF_EVEN))


def compute_attribution(session: Session, *, year: int, month: int) -> Attribution:
    """The Doc 04 §14 monthly attribution over committed tables. Read-only:
    safe to call from the API surface at any time, including mid-cycle.

    Fail-closed (ADR-0018): refuses if the authoritative book holds any open lot
    attributable to a non-authoritative strategy — a whole-book number must never
    silently include research_shadow / non-authoritative capital."""
    assert_authoritative_book(session)
    start, end = _month_bounds(year, month)
    bounds = {"s": start, "e": end}

    fills = session.execute(text(
        "SELECT o.side, e.fill_qty, e.decision_price, e.shortfall_bps, e.fx_rate_used "
        "FROM trading.executions e JOIN trading.orders o ON o.id = e.order_id "
        "WHERE e.executed_at >= :s AND e.executed_at < :e"), bounds).all()
    entry = _shortfall_line([f for f in fills if f.side == "buy"])
    exit_ = _shortfall_line([f for f in fills if f.side == "sell"])

    lots = session.execute(text(
        "SELECT count(*), COALESCE(SUM(proceeds_aud - cost_aud), 0) "
        "FROM trading.tax_lots WHERE disposed_at >= :s AND disposed_at < :e"),
        bounds).one()
    lots_closed, realised = int(lots[0]), Decimal(lots[1]).quantize(_CENT)

    # boundary snapshots (module docstring): latest-before falls back to
    # earliest-inside; the end is always the latest INSIDE the period. `id`
    # breaks pathological as_of ties deterministically.
    start_row = session.execute(text(
        "SELECT id, nav_aud FROM trading.portfolio_snapshots WHERE as_of < :s "
        "ORDER BY as_of DESC, id DESC LIMIT 1"), bounds).first()
    if start_row is None:
        start_row = session.execute(text(
            "SELECT id, nav_aud FROM trading.portfolio_snapshots "
            "WHERE as_of >= :s AND as_of < :e "
            "ORDER BY as_of ASC, id ASC LIMIT 1"), bounds).first()
    end_row = session.execute(text(
        "SELECT id, nav_aud FROM trading.portfolio_snapshots "
        "WHERE as_of >= :s AND as_of < :e "
        "ORDER BY as_of DESC, id DESC LIMIT 1"), bounds).first()
    nav_start: Decimal | None = None
    nav_end: Decimal | None = None
    swing: Decimal | None = None
    if start_row is not None and end_row is not None and start_row.id != end_row.id:
        nav_start = Decimal(start_row.nav_aud).quantize(_CENT)
        nav_end = Decimal(end_row.nav_aud).quantize(_CENT)
        swing = (nav_end - nav_start).quantize(_CENT)

    spend = session.execute(text(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM research.agent_runs "
        "WHERE created_at >= :s AND created_at < :e"), bounds).scalar_one()

    return Attribution(
        period=f"{year:04d}-{month:02d}",
        trades_buy=entry.fills, trades_sell=exit_.fills,
        entry_shortfall=entry, exit_shortfall=exit_,
        realised_pnl_aud=realised, lots_closed=lots_closed,
        nav_start_aud=nav_start, nav_end_aud=nav_end,
        unrealised_swing_aud=swing,
        llm_spend_usd=Decimal(spend).quantize(_USD4))
