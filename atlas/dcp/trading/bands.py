"""Daily tolerance-band check for approved strategies (ADR-0010 guardrails).

Each cycle, after the book snapshot, this module writes one quant.sleeve_daily
row per banded strategy and enforces the strategy row's tolerance_bands:
- sleeve drawdown from its own peak worse than the recorded
  max_drawdown_from_sleeve_peak (ADR-0010: -0.40) -> demote to 'suspended';
- trailing-126-session sleeve excess vs SPY TOTAL RETURN below the recorded
  trailing_126_session_excess_vs_spy_tr_pp (ADR-0010: -25.0 pp) -> demote.
Demotion is machine-executed and LATCHING: a suspended strategy is never
re-evaluated for demotion and NOTHING here (or anywhere in code) re-promotes
it — re-promotion is a Principal signature (ADR-0010). The demotion emits an
audit event (dcp actor, band values in the payload) and pages the operator
through atlas/ops/alerts.py.

WHY THIS LIVES UNDER dcp/trading AND NOT dcp/risk (deliberate): the band
check is strategy-LIFECYCLE governance — it grades a sleeve's recorded series
and flips a quant.strategies state — not an order-path risk check. The risk
engine's L1-L11 (`make cov-risk` 100% branch gate) stays exactly as signed
in Phase 4; placing this here keeps that gate's scope honest instead of
quietly widening it. Two-plane wall unaffected: pure DCP reads/writes, no
agent import, injectable Clock only.

SLEEVE ATTRIBUTION (the join, stated once): a tax lot belongs to a strategy's
sleeve iff its execution's order's proposal carries any of the strategy's
quant.signals ids in trade_proposals.signal_ids (the bridge writes REAL
signal UUIDs for signal-backed memos). Then, per the ADR-0010 wiring spec:

    sleeve_value = market value of OPEN sleeve lots (qty x latest vendor
                   close on or before the session x FX->AUD)
                 + cumulative REALISED PnL of disposed sleeve lots
                   (proceeds_aud - cost_aud).

Documented semantics and edges:
- EMPTY SLEEVE (no sleeve lot has ever existed): the row records
  sleeve_value NULL — "not initiated", explicitly NOT zero — and no band can
  breach. peak/drawdown/excess stay NULL.
- The value series is PnL-anchored on the realised side: a sell replaces a
  lot's market value with only its realised PnL, so a LARGE PROFITABLE
  LIQUIDATION steps the series down by the returned capital and can read as
  drawdown. That error direction is conservative (it can only cause a FALSE
  demotion, never a missed one; suspension is fail-safe and human-reversible)
  and is accepted for v1 — the approval-contract build (board item 7) owns
  the refinement to full sub-account accounting.
- DRAWDOWN: running peak = max(previous peak, value) over non-NULL rows;
  drawdown = value/peak - 1, NULL until the peak is positive.
- EXCESS (dormant by design until 126 sleeve sessions exist — ADR-0010 band
  2 is a 126-session statistic and must not fire on a shorter base): computed
  once 126 PRIOR non-NULL sleeve rows exist, as
  (value_t/value_{t-126} - 1)*100 - (spy_tr_t/spy_tr_{t-126} - 1)*100, both
  legs from STORED rows (replayable; never recomputed history). SPY TR closes
  come from stored SPY vendor bars + dividends via the total-return transform
  (prefix-causal, so today's computation extends yesterday's, never revises
  it). Missing SPY data stores NULL and leaves the excess dormant — the DD
  band still enforces.
- A strategy in 'suspended' keeps its sleeve series recorded (the record must
  not stop when the strategy does) but is never demoted again.
- Malformed/missing tolerance_bands on a paper/live strategy RAISES: a banded
  approval without enforceable bands is a governance breach, not a skip.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.execution.paper import PRICE_SOURCE, fx_to_aud
from atlas.dcp.market_data.calendars import last_completed_session
from atlas.dcp.market_data.total_return import (
    load_adjusted_dividends,
    total_return_series,
)
from atlas.dcp.trading.proposals import _latest_close
from atlas.ops.alerts import notify

BENCHMARK = "SPY"
EXCESS_SESSIONS = 126                    # ADR-0010: trailing 126-session excess
DD_BAND_KEY = "max_drawdown_from_sleeve_peak"
EXCESS_BAND_KEY = "trailing_126_session_excess_vs_spy_tr_pp"


@dataclass(frozen=True)
class StrategyBandStatus:
    family: str
    state: str                           # state BEFORE this check ran
    session: date
    sleeve_value: Decimal | None         # None = sleeve never initiated
    peak: Decimal | None
    drawdown: float | None
    excess_pp: float | None              # None while dormant (< 126 sessions)
    action: str                          # ok | empty | demoted | latched

    def line(self) -> str:
        if self.sleeve_value is None:
            return f"{self.family}: sleeve empty"
        dd = "n/a" if self.drawdown is None else f"{self.drawdown:.4f}"
        ex = "dormant" if self.excess_pp is None else f"{self.excess_pp:.2f}pp"
        return (f"{self.family}: value {self.sleeve_value} dd {dd} "
                f"excess {ex} -> {self.action}")


@dataclass(frozen=True)
class BandReport:
    statuses: tuple[StrategyBandStatus, ...] = ()

    def summary(self) -> str:
        if not self.statuses:
            return "bands idle (no banded strategy)"
        return " · ".join(s.line() for s in self.statuses)


def _sleeve_lots(session: Session, strategy_id: UUID) -> tuple[
        list[tuple[UUID, int, str]], Decimal, bool]:
    """(open lots as (instrument_id, qty, currency), realised PnL AUD,
    sleeve_exists). A lot is in the sleeve iff its proposal's signal_ids
    intersect the strategy's quant.signals ids (module docstring)."""
    rows = session.execute(text(
        "SELECT tp.instrument_id, i.currency, tl.qty, tl.cost_aud, "
        "       tl.proceeds_aud, tl.disposed_at "
        "FROM trading.tax_lots tl "
        "JOIN trading.executions e ON e.id = tl.execution_id "
        "JOIN trading.orders o ON o.id = e.order_id "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "JOIN market.instruments i ON i.id = tp.instrument_id "
        "WHERE tp.signal_ids && ARRAY(SELECT id FROM quant.signals "
        "                             WHERE strategy_id = :sid)")
        , {"sid": strategy_id}).all()
    open_lots: list[tuple[UUID, int, str]] = []
    realised = Decimal("0")
    for r in rows:
        if r.disposed_at is None:
            open_lots.append((r.instrument_id, int(r.qty), str(r.currency)))
        else:
            proceeds = Decimal(r.proceeds_aud) if r.proceeds_aud is not None \
                else Decimal("0")
            realised += proceeds - Decimal(r.cost_aud)
    return open_lots, realised, bool(rows)


def _spy_tr_close(session: Session, on: date) -> Decimal | None:
    """SPY total-return close as of the last SPY vendor bar at or before `on`
    (dividends reinvested at ex-date close — market_data/total_return.py); the
    transform is prefix-causal so stored history never gets revised. None when
    SPY bars are absent (excess stays dormant; DD still enforces)."""
    rows = session.execute(text(
        "SELECT pb.bar_date, pb.close FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol = :sym AND pb.source = :src AND pb.bar_date <= :on "
        "  AND pb.close IS NOT NULL ORDER BY pb.bar_date"),
        {"sym": BENCHMARK, "src": PRICE_SOURCE, "on": on}).all()
    if not rows:
        return None
    closes = [float(r.close) for r in rows]
    trs = total_return_series(
        dates=[r.bar_date for r in rows], opens=list(closes),
        closes=closes,
        dividends=[d for d in load_adjusted_dividends(session, BENCHMARK)
                   if d.ex_date <= on])
    return Decimal(str(trs.closes[-1]))


def _prior_rows(session: Session, strategy_id: UUID, before: date,
                ) -> tuple[Decimal | None, tuple[Decimal, Decimal | None] | None]:
    """(previous peak, (value, spy_tr) at EXCESS_SESSIONS sessions back) from
    the stored non-NULL sleeve series strictly before `before`."""
    prev = session.execute(text(
        "SELECT peak_value FROM quant.sleeve_daily "
        "WHERE strategy_id = :sid AND session_date < :d "
        "  AND sleeve_value IS NOT NULL "
        "ORDER BY session_date DESC LIMIT 1"),
        {"sid": strategy_id, "d": before}).first()
    base = session.execute(text(
        "SELECT sleeve_value, spy_tr_close FROM quant.sleeve_daily "
        "WHERE strategy_id = :sid AND session_date < :d "
        "  AND sleeve_value IS NOT NULL "
        "ORDER BY session_date DESC OFFSET :off LIMIT 1"),
        {"sid": strategy_id, "d": before, "off": EXCESS_SESSIONS - 1}).first()
    prev_peak = Decimal(prev.peak_value) if prev is not None \
        and prev.peak_value is not None else None
    base_row = None
    if base is not None:
        base_row = (Decimal(base.sleeve_value),
                    Decimal(base.spy_tr_close)
                    if base.spy_tr_close is not None else None)
    return prev_peak, base_row


def _band_limits(family: str, state: str, bands: Any) -> tuple[float, float]:
    """Fail-closed read of the tolerance_bands JSON (module docstring)."""
    if not isinstance(bands, dict):
        raise RuntimeError(f"{family} ({state}) has no tolerance_bands object "
                           "— a banded approval without bands is a governance "
                           "breach (ADR-0010)")
    try:
        return float(bands[DD_BAND_KEY]), float(bands[EXCESS_BAND_KEY])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"{family} ({state}) tolerance_bands missing or "
                           f"malformed ({e!r}) — refusing to run unbanded "
                           "(ADR-0010)") from e


def _upsert_row(session: Session, clock: Clock, *, strategy_id: UUID,
                on: date, value: Decimal | None, spy_tr: Decimal | None,
                peak: Decimal | None, dd: float | None,
                excess: float | None) -> None:
    session.execute(text(
        "INSERT INTO quant.sleeve_daily (strategy_id, session_date, sleeve_value, "
        " spy_tr_close, peak_value, drawdown, excess_126s_pp, created_at) "
        "VALUES (:sid, :d, :v, :spy, :pk, :dd, :ex, :ca) "
        "ON CONFLICT (strategy_id, session_date) DO UPDATE SET "
        " sleeve_value = :v, spy_tr_close = :spy, peak_value = :pk, "
        " drawdown = :dd, excess_126s_pp = :ex"),
        {"sid": strategy_id, "d": on, "v": value, "spy": spy_tr, "pk": peak,
         "dd": None if dd is None else Decimal(str(dd)),
         "ex": None if excess is None else Decimal(str(excess)),
         "ca": clock.now()})


def check_bands(session: Session, clock: Clock) -> BandReport:
    """Record the sleeve series and enforce the tolerance bands for every
    strategy in paper/live (suspended: recorded, never re-demoted). Called by
    the daily cycle after the snapshot; safe to run manually — same-session
    re-runs recompute the same row deterministically."""
    now = clock.now()
    on = last_completed_session("US", now)
    strategies = session.execute(text(
        "SELECT id, family, state, tolerance_bands FROM quant.strategies "
        "WHERE state IN ('paper','live','suspended') "
        "ORDER BY family, created_at")).all()

    audit = PostgresAuditLog(session, clock)
    statuses: list[StrategyBandStatus] = []
    for st in strategies:
        strategy_id: UUID = st.id
        family, state = str(st.family), str(st.state)
        enforce = state in ("paper", "live")
        dd_limit, excess_limit = _band_limits(family, state,
                                              st.tolerance_bands)

        open_lots, realised, sleeve_exists = _sleeve_lots(session, strategy_id)
        spy_tr = _spy_tr_close(session, on)
        if not sleeve_exists:
            _upsert_row(session, clock, strategy_id=strategy_id, on=on,
                        value=None, spy_tr=spy_tr, peak=None, dd=None,
                        excess=None)
            statuses.append(StrategyBandStatus(
                family=family, state=state, session=on, sleeve_value=None,
                peak=None, drawdown=None, excess_pp=None, action="empty"))
            continue

        fx_cache: dict[str, Decimal] = {}
        mv = Decimal("0")
        for iid, qty, currency in open_lots:
            if currency not in fx_cache:
                fx_cache[currency] = fx_to_aud(session, currency, on)
            mv += Decimal(qty) * _latest_close(session, iid, on) \
                * fx_cache[currency]
        value = mv + realised

        prev_peak, base_row = _prior_rows(session, strategy_id, on)
        peak = value if prev_peak is None else max(prev_peak, value)
        dd = float(value / peak - 1) if peak > 0 else None
        excess: float | None = None
        if (base_row is not None and base_row[0] > 0 and base_row[1] is not None
                and base_row[1] > 0 and spy_tr is not None):
            sleeve_ret = float(value / base_row[0] - 1) * 100
            spy_ret = float(spy_tr / base_row[1] - 1) * 100
            excess = sleeve_ret - spy_ret

        _upsert_row(session, clock, strategy_id=strategy_id, on=on,
                    value=value, spy_tr=spy_tr, peak=peak, dd=dd,
                    excess=excess)

        dd_breach = dd is not None and dd < dd_limit
        excess_breach = excess is not None and excess < excess_limit
        if enforce and (dd_breach or excess_breach):
            session.execute(text(
                "UPDATE quant.strategies SET state = 'suspended' "
                "WHERE id = :sid AND state IN ('paper','live')"),
                {"sid": strategy_id})
            payload = {"strategy_id": str(strategy_id), "family": family,
                       "from_state": state, "to_state": "suspended",
                       "session": on.isoformat(),
                       "sleeve_value": str(value), "peak": str(peak),
                       "drawdown": dd, "dd_limit": dd_limit,
                       "excess_126s_pp": excess, "excess_limit": excess_limit,
                       "dd_breach": dd_breach, "excess_breach": excess_breach,
                       "latching": True,
                       "repromotion": "Principal signature only (ADR-0010)"}
            audit.append(event_type="quant.strategy.demoted",
                         entity_type="strategy", entity_id=str(strategy_id),
                         actor_type="dcp", actor_id="band_check",
                         payload=payload)
            notify(f"Atlas band breach: {family} DEMOTED to suspended",
                   f"session {on}: drawdown {dd} (limit {dd_limit}), "
                   f"excess {excess}pp (limit {excess_limit}pp) — latching; "
                   "re-promotion is a Principal signature (ADR-0010)",
                   priority="high")
            action = "demoted"
        elif not enforce:
            action = "latched"           # suspended: recorded, never re-demoted
        else:
            action = "ok"
        statuses.append(StrategyBandStatus(
            family=family, state=state, session=on, sleeve_value=value,
            peak=peak, drawdown=dd, excess_pp=excess, action=action))
    return BandReport(statuses=tuple(statuses))
