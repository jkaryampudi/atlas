"""Daily core/satellite attribution + the monthly report (ADR-0012 cons. 4).

Doc 04 §14's letter is the implementation-shortfall standing line (computed by
atlas/dcp/portfolio/attribution.py and embedded in the monthly report below).
ADR-0012 consequence 4 signs the rest: "reporting/attribution must separate
core (beta) from satellite (alpha) so the scorecard measures what the active
strategies actually add over simply holding the index." This module is that
separation — deterministic DCP computation only, injectable clock, upserts
into reporting.attribution_daily (migration 0027), audit event per generated
monthly report.

THE DECOMPOSITION (per US session, anchored on trading.portfolio_snapshots):
each session's book is the LATEST snapshot whose last completed US session is
that date; with its as_of instant T and nav N:

    core   = market value of open tax lots of is_core positions at T,
             marked qty x latest vendor close on/before the session x FX->AUD
             (the same fail-closed marks the snapshot itself uses)
    xsmom  = market value of open satellite lots at T via THE sleeve join
    pead     (bands.SLEEVE_LOTS_JOIN: lot -> execution -> order -> proposal
             .signal_ids && the family's quant.signals), families
             xsmom-pit-tr / pead-sue-tr
    cash   = N minus the sleeves — the residual. It equals ledger cash today;
             it would silently absorb any holding the joins cannot attribute,
             which v1 structurally cannot produce (every satellite settlement
             carries signal ids; every core settlement sets is_core). The
             closed sleeve CHECK makes a third sleeve a signed change.
    total  = N

    VALUE IDENTITY (exact, by construction): core+xsmom+pead+cash == total.

Deliberate difference from quant.sleeve_daily: that series is PnL-anchored
(realised PnL stays in the sleeve) because it GRADES a strategy against its
approval bands; this one is a NAV decomposition (realised proceeds move to
cash) because it explains the book. Empty sleeve here is A$0, not NULL.

THE FLOW-ADJUSTMENT CONVENTION (stated once, pinned by golden tests): a fill
moves capital cash<->sleeve, so a naive value diff would book a buy as a
"return". For sleeve s on session d, with V = value, P = previous STORED
value, over the flow window (prev snapshot as_of, this snapshot as_of]:

    inflow  F_in  = SUM(tax_lots.cost_aud)     acquired in the window
    outflow F_out = SUM(tax_lots.proceeds_aud) disposed in the window
    base          = P + F_in
    ret_1d        = (V - P - F_in + F_out) / base        [NULL if base <= 0]

i.e. flows are treated as occurring AT THE SESSION OPEN (paper fills ARE
next-session-open fills, stop exits intraday): capital bought in counts in
the day's base; capital sold out leaves the base with its proceeds credited
back to the numerator. Both legs use the BOOKED lot amounts (cost_aud /
proceeds_aud = qty x fill price x FX, fees excluded), which are exactly the
amounts that cross the cash<->sleeve boundary — fees therefore surface as
cash's negative contribution, never as sleeve performance. ret_1d is NULL on
the first stored session (a return needs two observations, never fabricated).
cash's own ret_1d is identically 0 once a prior row exists (v1: no interest
model — every cash move is a flow); total's is N/N_prev - 1 (a paper fund has
no external flows after the seed).

CONTRIBUTION (the exact AUD decomposition of the NAV change, report-only —
recomputable, not stored):

    invested sleeve:  C_s = (V - P) - (F_in - F_out)     [its P&L]
    cash:             C_cash = (cash - prev_cash) + SUM over invested sleeves
                               of (F_in - F_out)          [= -fees in v1]
    IDENTITY (exact): C_core + C_xsmom + C_pead + C_cash == N - N_prev.

BENCHMARKS (benchmark_ret_1d, same two sessions as ret_1d):
    core           the SIGNED ADR-0012 target weights SPY 55 / INDA 15,
                   renormalized to a fully-invested 55:15 blend —
                   (0.55*r_SPY + 0.15*r_INDA) / 0.70 — both legs TOTAL RETURN
                   (dividends reinvested at ex-date close, the one convention
                   in market_data/total_return.py); NULL if either leg's bar
                   is missing
    xsmom/pead     SPY total return (ADR-0009: the market IS the honest
    and total      alternative)
    cash           0
A benchmark is a property of the day and sleeve, recorded even for an empty
sleeve; NULL when its bars are absent, never a guess.

SATELLITE ALPHA (the honest number): the satellite composite treats
xsmom+pead as one sleeve (values summed, flows summed, same ret convention).
alpha_pp = (compounded satellite return - compounded SPY TR return) x 100
over every stored session where BOTH legs exist (same days, both legs — no
mismatched windows). Near-zero history today; the machinery accrues.

IDEMPOTENCY: re-running any day upserts byte-identical rows (previous-day
inputs come from STORED rows, the bands.py replayability convention;
created_at is set on first insert and never updated). --backfill walks every
snapshot session forward through the same code path.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock, FrozenClock, SystemClock
from atlas.dcp.execution.paper import PRICE_SOURCE, fx_to_aud
from atlas.dcp.market_data.calendars import last_completed_session
from atlas.dcp.market_data.total_return import (
    load_adjusted_dividends,
    total_return_series,
)
from atlas.dcp.portfolio.attribution import Attribution, compute_attribution
from atlas.dcp.trading.bands import BENCHMARK, SLEEVE_LOTS_JOIN
from atlas.dcp.trading.core_allocation import CORE_TARGETS
from atlas.dcp.trading.proposals import _latest_close

CORE, XSMOM, PEAD, CASH, TOTAL = "core", "xsmom", "pead", "cash", "total"
SLEEVES = (CORE, XSMOM, PEAD, CASH, TOTAL)
INVESTED = (CORE, XSMOM, PEAD)
# the signed strategy rows behind each satellite sleeve (ADR-0010 / ADR-0013);
# generate.py modules own the family strings — mirrored here as the closed
# sleeve->family map the 0027 CHECK constraint encodes
SATELLITE_FAMILIES: dict[str, str] = {XSMOM: "xsmom-pit-tr", PEAD: "pead-sue-tr"}
INDIA_LEG = "INDA"                      # ADR-0012 core India leg

_CENT = Decimal("0.01")
_RET8 = Decimal("0.00000001")           # stored return precision (8dp fraction)
_PP2 = Decimal("0.01")                  # percentage points, 2dp
MONTHLY_REPORT_EVENT = "reporting.attribution.monthly"

_CORE_LOTS_SQL = (
    "SELECT p.instrument_id, i.currency, tl.qty, tl.cost_aud, tl.proceeds_aud, "
    "       tl.acquired_at, tl.disposed_at "
    "FROM trading.tax_lots tl "
    "JOIN trading.positions p ON p.id = tl.position_id "
    "JOIN market.instruments i ON i.id = p.instrument_id "
    "WHERE p.is_core")

# the SAME join bands.py states once (imported, never forked) with the lot
# lifecycle columns this module needs for point-in-time values and flows
_SLEEVE_LOTS_SQL = (
    "SELECT tp.instrument_id, i.currency, tl.qty, tl.cost_aud, tl.proceeds_aud, "
    "       tl.acquired_at, tl.disposed_at "
    + SLEEVE_LOTS_JOIN)


# --------------------------------------------------------------- pure helpers

def flow_adjusted_return(value: Decimal, prev_value: Decimal | None,
                         inflow: Decimal, outflow: Decimal) -> Decimal | None:
    """THE convention (module docstring): flows at the session open. None when
    there is no prior observation or no base — never a fabricated number."""
    if prev_value is None:
        return None
    base = prev_value + inflow
    if base <= 0:
        return None
    pnl = value - prev_value - inflow + outflow
    return (pnl / base).quantize(_RET8, rounding=ROUND_HALF_EVEN)


def core_blend_return(spy_ret: Decimal | None,
                      inda_ret: Decimal | None) -> Decimal | None:
    """The signed ADR-0012 core benchmark: target weights SPY 55 / INDA 15
    renormalized to a fully-invested 55:15 blend. None if either leg is
    missing — a one-legged blend would misgrade the core silently."""
    if spy_ret is None or inda_ret is None:
        return None
    w_spy, w_inda = CORE_TARGETS["SPY"], CORE_TARGETS[INDIA_LEG]
    blended = (w_spy * spy_ret + w_inda * inda_ret) / (w_spy + w_inda)
    return blended.quantize(_RET8, rounding=ROUND_HALF_EVEN)


# ------------------------------------------------------------------- loaders

@dataclass(frozen=True)
class _Snap:
    session: date                       # last completed US session at as_of
    as_of: datetime
    nav_aud: Decimal


def _session_snapshots(session: Session) -> list[_Snap]:
    """Latest snapshot per derived US session, ascending. A weekend/manual
    re-snapshot of the same session supersedes the earlier one (same book,
    fresher marks) — the upsert then re-records the session identically."""
    by_session: dict[date, _Snap] = {}
    for r in session.execute(text(
            "SELECT as_of, nav_aud FROM trading.portfolio_snapshots "
            "ORDER BY as_of, id")):
        d = last_completed_session("US", r.as_of)
        by_session[d] = _Snap(session=d, as_of=r.as_of,
                              nav_aud=Decimal(r.nav_aud).quantize(_CENT))
    return [by_session[d] for d in sorted(by_session)]


@dataclass(frozen=True)
class _Lot:
    instrument_id: UUID
    currency: str
    qty: int
    cost_aud: Decimal
    proceeds_aud: Decimal
    acquired_at: datetime
    disposed_at: datetime | None


def _lots(rows: list[Any]) -> list[_Lot]:
    return [_Lot(instrument_id=r.instrument_id, currency=str(r.currency),
                 qty=int(r.qty), cost_aud=Decimal(r.cost_aud),
                 proceeds_aud=(Decimal(r.proceeds_aud)
                               if r.proceeds_aud is not None else Decimal(0)),
                 acquired_at=r.acquired_at, disposed_at=r.disposed_at)
            for r in rows]


def _ledger(session: Session) -> dict[str, list[_Lot]]:
    """Every attributable lot, by sleeve. Core by the positive is_core marker
    (ADR-0014); satellites by the bands.py join per family strategy row (all
    states — a suspended or retired strategy's lots are still book history)."""
    ledger: dict[str, list[_Lot]] = {
        CORE: _lots(list(session.execute(text(_CORE_LOTS_SQL))))}
    for sleeve, family in SATELLITE_FAMILIES.items():
        rows: list[Any] = []
        for st in session.execute(text(
                "SELECT id FROM quant.strategies WHERE family = :f "
                "ORDER BY created_at, id"), {"f": family}):
            rows.extend(session.execute(text(_SLEEVE_LOTS_SQL),
                                        {"sid": st.id}))
        ledger[sleeve] = _lots(rows)
    return ledger


def _mark_value(session: Session, lots: list[_Lot], at: datetime,
                on: date) -> Decimal:
    """Market value in AUD of the lots OPEN at instant `at`, marked at session
    `on`. Fail-closed via _latest_close/fx_to_aud: a holding that cannot be
    marked raises — the cycle node pages, it never guesses."""
    fx_cache: dict[str, Decimal] = {}
    px_cache: dict[UUID, Decimal] = {}
    value = Decimal(0)
    for lot in lots:
        if lot.acquired_at > at or (lot.disposed_at is not None
                                    and lot.disposed_at <= at):
            continue
        if lot.currency not in fx_cache:
            fx_cache[lot.currency] = fx_to_aud(session, lot.currency, on)
        if lot.instrument_id not in px_cache:
            px_cache[lot.instrument_id] = _latest_close(
                session, lot.instrument_id, on)
        value += Decimal(lot.qty) * px_cache[lot.instrument_id] \
            * fx_cache[lot.currency]
    return value.quantize(_CENT, rounding=ROUND_HALF_EVEN)


def _flows(lots: list[_Lot], start: datetime | None,
           end: datetime) -> tuple[Decimal, Decimal]:
    """(inflow, outflow) over the half-open window (start, end] in BOOKED lot
    amounts (module docstring). start None = beginning of time."""
    inflow = outflow = Decimal(0)
    for lot in lots:
        if lot.acquired_at <= end and (start is None or lot.acquired_at > start):
            inflow += lot.cost_aud
        if lot.disposed_at is not None and lot.disposed_at <= end \
                and (start is None or lot.disposed_at > start):
            outflow += lot.proceeds_aud
    return inflow, outflow


def _tr_ret(session: Session, symbol: str, on: date,
            prev: date) -> Decimal | None:
    """TOTAL-RETURN close-to-close return of `symbol` between the two exact
    session dates, via the one prefix-causal transform in
    market_data/total_return.py. None unless BOTH dates have vendor bars."""
    rows = session.execute(text(
        "SELECT pb.bar_date, pb.close FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol = :sym AND pb.source = :src AND pb.bar_date <= :on "
        "  AND pb.close IS NOT NULL ORDER BY pb.bar_date"),
        {"sym": symbol, "src": PRICE_SOURCE, "on": on}).all()
    dates = [r.bar_date for r in rows]
    if not dates or dates[-1] != on or prev not in dates:
        return None
    closes = [float(r.close) for r in rows]
    trs = total_return_series(
        dates=dates, opens=list(closes), closes=closes,
        dividends=[d for d in load_adjusted_dividends(session, symbol)
                   if d.ex_date <= on])
    tr_on = Decimal(str(trs.closes[-1]))
    tr_prev = Decimal(str(trs.closes[dates.index(prev)]))
    if tr_prev <= 0:
        return None
    return tr_on / tr_prev - 1


# ------------------------------------------------------- the daily attribution

@dataclass(frozen=True)
class SleeveDay:
    sleeve: str
    value_aud: Decimal                  # cents; A$0 is a real empty-sleeve value
    ret_1d: Decimal | None              # 8dp fraction, flow-adjusted
    benchmark_ret_1d: Decimal | None    # 8dp fraction
    flow_in_aud: Decimal                # window flows (report-only, recomputable)
    flow_out_aud: Decimal
    contribution_aud: Decimal | None    # exact AUD share of the NAV change


@dataclass(frozen=True)
class AttributionDayReport:
    session: date
    rows: tuple[SleeveDay, ...]         # core, xsmom, pead, cash, total
    satellite_ret_1d: Decimal | None    # xsmom+pead as one sleeve, same convention
    spy_ret_1d: Decimal | None          # the satellite/total yardstick that day
    alpha_pp: Decimal | None            # cumulative satellite alpha (module docstring)

    def by_sleeve(self) -> dict[str, SleeveDay]:
        return {r.sleeve: r for r in self.rows}

    def summary(self) -> str:
        by = self.by_sleeve()

        def pct(v: Decimal | None) -> str:
            return "n/a" if v is None else f"{v * 100:+.2f}%"

        alpha = "n/a" if self.alpha_pp is None else f"{self.alpha_pp:+.2f}pp"
        return (f"attribution: core {pct(by[CORE].ret_1d)} "
                f"vs blend {pct(by[CORE].benchmark_ret_1d)} "
                f"· satellite {pct(self.satellite_ret_1d)} "
                f"vs SPY {pct(self.spy_ret_1d)} "
                f"· alpha {alpha} cumulative")


def _prev_stored(session: Session,
                 before: date) -> tuple[date | None, dict[str, Decimal]]:
    """(previous stored session, its per-sleeve values). Stored rows only —
    the replayability convention: recomputing today never revises history."""
    prev = session.execute(text(
        "SELECT max(session_date) FROM reporting.attribution_daily "
        "WHERE session_date < :d"), {"d": before}).scalar()
    if prev is None:
        return None, {}
    vals = {str(r.sleeve): Decimal(r.value_aud) for r in session.execute(text(
        "SELECT sleeve, value_aud FROM reporting.attribution_daily "
        "WHERE session_date = :d"), {"d": prev})}
    return prev, vals


def _upsert(session: Session, clock: Clock, on: date, row: SleeveDay) -> None:
    session.execute(text(
        "INSERT INTO reporting.attribution_daily "
        " (session_date, sleeve, value_aud, ret_1d, benchmark_ret_1d, created_at) "
        "VALUES (:d, :s, :v, :r, :b, :ca) "
        "ON CONFLICT (session_date, sleeve) DO UPDATE SET "
        " value_aud = :v, ret_1d = :r, benchmark_ret_1d = :b"),
        {"d": on, "s": row.sleeve, "v": row.value_aud, "r": row.ret_1d,
         "b": row.benchmark_ret_1d, "ca": clock.now()})


def _compute_for(session: Session, clock: Clock, snaps: list[_Snap], idx: int,
                 ledger: dict[str, list[_Lot]]) -> AttributionDayReport:
    """Decompose one snapshot session and upsert its five rows."""
    snap = snaps[idx]
    d, at, nav = snap.session, snap.as_of, snap.nav_aud
    prev_session, prev_vals = _prev_stored(session, d)
    window_start: datetime | None = None
    if prev_session is not None:
        anchors = [s.as_of for s in snaps if s.session == prev_session]
        if not anchors:
            raise RuntimeError(
                f"attribution: stored session {prev_session} has no snapshot "
                "anchor — the snapshot series and the attribution series have "
                "diverged; refusing to guess a flow window")
        window_start = anchors[-1]

    values: dict[str, Decimal] = {
        s: _mark_value(session, ledger[s], at, d) for s in INVESTED}
    values[CASH] = nav - sum((values[s] for s in INVESTED), Decimal(0))
    values[TOTAL] = nav

    flows: dict[str, tuple[Decimal, Decimal]] = {
        s: (_flows(ledger[s], window_start, at) if prev_session is not None
            else (Decimal(0), Decimal(0)))
        for s in INVESTED}

    spy = inda = None
    if prev_session is not None:
        spy = _tr_ret(session, BENCHMARK, d, prev_session)
        inda = _tr_ret(session, INDIA_LEG, d, prev_session)
    spy8 = spy.quantize(_RET8, rounding=ROUND_HALF_EVEN) if spy is not None else None
    benchmarks: dict[str, Decimal | None] = {
        CORE: core_blend_return(spy, inda), XSMOM: spy8, PEAD: spy8,
        TOTAL: spy8,
        CASH: Decimal(0).quantize(_RET8) if prev_session is not None else None}

    rets: dict[str, Decimal | None] = {}
    contribs: dict[str, Decimal | None] = {}
    net_flows = Decimal(0)
    for s in INVESTED:
        f_in, f_out = flows[s]
        net_flows += f_in - f_out
        rets[s] = flow_adjusted_return(values[s], prev_vals.get(s), f_in, f_out)
        contribs[s] = (values[s] - prev_vals[s] - (f_in - f_out)
                       if s in prev_vals else None)
    prev_cash, prev_total = prev_vals.get(CASH), prev_vals.get(TOTAL)
    rets[CASH] = Decimal(0).quantize(_RET8) if prev_cash is not None else None
    contribs[CASH] = (values[CASH] - prev_cash + net_flows
                      if prev_cash is not None else None)
    rets[TOTAL] = ((nav / prev_total - 1).quantize(_RET8, rounding=ROUND_HALF_EVEN)
                   if prev_total is not None and prev_total > 0 else None)
    contribs[TOTAL] = nav - prev_total if prev_total is not None else None

    rows = tuple(SleeveDay(
        sleeve=s, value_aud=values[s], ret_1d=rets[s],
        benchmark_ret_1d=benchmarks[s],
        flow_in_aud=flows.get(s, (Decimal(0), Decimal(0)))[0],
        flow_out_aud=flows.get(s, (Decimal(0), Decimal(0)))[1],
        contribution_aud=contribs[s]) for s in SLEEVES)
    for row in rows:
        _upsert(session, clock, d, row)

    sat_in = flows[XSMOM][0] + flows[PEAD][0]
    sat_out = flows[XSMOM][1] + flows[PEAD][1]
    sat_prev = (prev_vals[XSMOM] + prev_vals[PEAD]
                if XSMOM in prev_vals and PEAD in prev_vals else None)
    sat_ret = flow_adjusted_return(values[XSMOM] + values[PEAD], sat_prev,
                                   sat_in, sat_out)
    return AttributionDayReport(
        session=d, rows=rows, satellite_ret_1d=sat_ret, spy_ret_1d=spy8,
        alpha_pp=cumulative_alpha_pp(session, snaps=snaps, ledger=ledger))


def compute_attribution_day(session: Session,
                            clock: Clock) -> AttributionDayReport | None:
    """The daily-cycle entry point: attribute the latest snapshot at or before
    the injected now. None when no snapshot exists yet (nothing to decompose).
    Safe to re-run — the upsert writes identical rows (module docstring)."""
    snaps = [s for s in _session_snapshots(session) if s.as_of <= clock.now()]
    if not snaps:
        return None
    return _compute_for(session, clock, snaps, len(snaps) - 1, _ledger(session))


def backfill_attribution(session: Session,
                         clock: Clock) -> list[AttributionDayReport]:
    """Compute every snapshot session forward from the earliest, through the
    exact daily code path (idempotent: a day already stored upserts
    identically). The series accrues from day one of the machinery."""
    snaps = [s for s in _session_snapshots(session) if s.as_of <= clock.now()]
    ledger = _ledger(session)
    return [_compute_for(session, clock, snaps, i, ledger)
            for i in range(len(snaps))]


# ----------------------------------------------- satellite alpha + cumulatives

def _stored_series(session: Session) -> list[tuple[date, dict[str, Any]]]:
    out: dict[date, dict[str, Any]] = {}
    for r in session.execute(text(
            "SELECT session_date, sleeve, value_aud, ret_1d, benchmark_ret_1d "
            "FROM reporting.attribution_daily "
            "ORDER BY session_date, sleeve")):
        out.setdefault(r.session_date, {})[str(r.sleeve)] = r
    return sorted(out.items())


def cumulative_alpha_pp(session: Session, *, snaps: list[_Snap] | None = None,
                        ledger: dict[str, list[_Lot]] | None = None,
                        ) -> Decimal | None:
    """Compounded satellite return minus compounded SPY TR, in percentage
    points, over every stored session where BOTH legs exist (module
    docstring). Satellite values and the SPY leg come from STORED rows; the
    flow adjustment is recomputed from the immutable lot ledger over the same
    snapshot windows that produced the rows. None with no measurable day."""
    series = _stored_series(session)
    if len(series) < 2:
        return None
    snaps = snaps if snaps is not None else _session_snapshots(session)
    ledger = ledger if ledger is not None else _ledger(session)
    as_of_by_session = {s.session: s.as_of for s in snaps}
    acc_r = acc_b = Decimal(1)
    measured = 0
    for i in range(1, len(series)):
        prev_d, prev_rows = series[i - 1]
        d, rows = series[i]
        if XSMOM not in rows or PEAD not in rows \
                or XSMOM not in prev_rows or PEAD not in prev_rows:
            continue
        w0, w1 = as_of_by_session.get(prev_d), as_of_by_session.get(d)
        if w0 is None or w1 is None:
            continue
        f_in = f_out = Decimal(0)
        for s in (XSMOM, PEAD):
            i_s, o_s = _flows(ledger[s], w0, w1)
            f_in, f_out = f_in + i_s, f_out + o_s
        prev_v = Decimal(prev_rows[XSMOM].value_aud) \
            + Decimal(prev_rows[PEAD].value_aud)
        v = Decimal(rows[XSMOM].value_aud) + Decimal(rows[PEAD].value_aud)
        r = flow_adjusted_return(v, prev_v, f_in, f_out)
        bench = rows[XSMOM].benchmark_ret_1d      # the shared SPY TR leg
        if r is None or bench is None:
            continue
        acc_r *= 1 + r
        acc_b *= 1 + Decimal(bench)
        measured += 1
    if measured == 0:
        return None
    return ((acc_r - acc_b) * 100).quantize(_PP2, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class SleeveCumulative:
    sleeve: str
    sessions: int                       # days compounded (both legs present)
    ret_pct: Decimal | None             # 2dp
    benchmark_pct: Decimal | None
    excess_pp: Decimal | None


def _compound(pairs: list[tuple[Decimal, Decimal]],
              sleeve: str) -> SleeveCumulative:
    acc_r = acc_b = Decimal(1)
    for r, b in pairs:
        acc_r *= 1 + r
        acc_b *= 1 + b
    if not pairs:
        return SleeveCumulative(sleeve=sleeve, sessions=0, ret_pct=None,
                                benchmark_pct=None, excess_pp=None)
    ret = ((acc_r - 1) * 100).quantize(_PP2, rounding=ROUND_HALF_EVEN)
    bench = ((acc_b - 1) * 100).quantize(_PP2, rounding=ROUND_HALF_EVEN)
    return SleeveCumulative(sleeve=sleeve, sessions=len(pairs), ret_pct=ret,
                            benchmark_pct=bench,
                            excess_pp=(ret - bench).quantize(_PP2))


def cumulative_by_sleeve(session: Session,
                         period: str | None = None) -> list[SleeveCumulative]:
    """Per-sleeve compounded return vs compounded benchmark over the stored
    rows (optionally one 'YYYY-MM' period) — only sessions where BOTH legs
    exist, so the comparison never mixes windows."""
    series = _stored_series(session)
    out: list[SleeveCumulative] = []
    for sleeve in SLEEVES:
        pairs: list[tuple[Decimal, Decimal]] = []
        for d, rows in series:
            if period is not None and d.isoformat()[:7] != period:
                continue
            row = rows.get(sleeve)
            if row is None or row.ret_1d is None \
                    or row.benchmark_ret_1d is None:
                continue
            pairs.append((Decimal(row.ret_1d), Decimal(row.benchmark_ret_1d)))
        out.append(_compound(pairs, sleeve))
    return out


# ------------------------------------------------------- the monthly report

@dataclass(frozen=True)
class SleeveMonth:
    sleeve: str
    sessions: int
    ret_pct: Decimal | None
    benchmark_pct: Decimal | None
    excess_pp: Decimal | None
    contribution_aud: Decimal           # exact AUD share of the month's NAV change
    end_value_aud: Decimal | None


@dataclass(frozen=True)
class MonthlyAttribution:
    period: str
    sleeves: tuple[SleeveMonth, ...]
    nav_change_aud: Decimal             # sum of total contributions = exact
    satellite_alpha_pp: Decimal | None  # since inception (module docstring)
    headline: str


def _month_contributions(session: Session,
                         period: str) -> tuple[dict[str, Decimal], Decimal]:
    """Exact AUD contributions per sleeve over the period's stored sessions
    (each day needs a prior stored session as its baseline — the same rule the
    daily ret_1d follows). Brinson-lite, allocation-only (v1, documented): the
    decomposition is by SLEEVE — C_s = deltaV_s - net flow (cash: +net flows) —
    which sums EXACTLY to the NAV change; no selection/interaction split."""
    series = _stored_series(session)
    snaps = _session_snapshots(session)
    as_of_by_session = {s.session: s.as_of for s in snaps}
    ledger = _ledger(session)
    contribs = {s: Decimal(0) for s in SLEEVES}
    nav_change = Decimal(0)
    for i in range(1, len(series)):
        d, rows = series[i]
        if d.isoformat()[:7] != period:
            continue
        prev_d, prev_rows = series[i - 1]
        if set(SLEEVES) - set(rows) or set(SLEEVES) - set(prev_rows):
            continue                     # partial day: not decomposable
        w0, w1 = as_of_by_session.get(prev_d), as_of_by_session.get(d)
        if w0 is None or w1 is None:
            continue
        net = Decimal(0)
        for s in INVESTED:
            f_in, f_out = _flows(ledger[s], w0, w1)
            net += f_in - f_out
            contribs[s] += (Decimal(rows[s].value_aud)
                            - Decimal(prev_rows[s].value_aud) - (f_in - f_out))
        contribs[CASH] += (Decimal(rows[CASH].value_aud)
                           - Decimal(prev_rows[CASH].value_aud) + net)
        day_total = (Decimal(rows[TOTAL].value_aud)
                     - Decimal(prev_rows[TOTAL].value_aud))
        contribs[TOTAL] += day_total
        nav_change += day_total
    return contribs, nav_change


def compute_monthly(session: Session, *, year: int,
                    month: int) -> MonthlyAttribution:
    """The ADR-0012 consequence-4 monthly view, entirely from stored rows +
    the immutable lot ledger. The honest one-liner grades the satellite
    against simply holding the index, since inception."""
    period = f"{year:04d}-{month:02d}"
    cumulative = {c.sleeve: c for c in cumulative_by_sleeve(session, period)}
    contribs, nav_change = _month_contributions(session, period)
    end_values: dict[str, Decimal] = {}
    for d, rows in _stored_series(session):
        if d.isoformat()[:7] == period:
            for s, r in rows.items():
                end_values[s] = Decimal(r.value_aud)
    sleeves = tuple(SleeveMonth(
        sleeve=s, sessions=cumulative[s].sessions,
        ret_pct=cumulative[s].ret_pct,
        benchmark_pct=cumulative[s].benchmark_pct,
        excess_pp=cumulative[s].excess_pp,
        contribution_aud=contribs[s].quantize(_CENT),
        end_value_aud=end_values.get(s)) for s in SLEEVES)
    alpha = cumulative_alpha_pp(session)
    if alpha is None:
        headline = ("The active satellite has no measurable history yet — "
                    "no session with both a satellite return and a SPY TR "
                    "benchmark. The machinery accrues; nothing is fabricated.")
    else:
        verb = "added" if alpha >= 0 else "subtracted"
        headline = (f"The active satellite {verb} {abs(alpha):.2f} pp vs "
                    "simply holding the index (SPY total return), "
                    "cumulative since inception.")
    return MonthlyAttribution(period=period, sleeves=sleeves,
                              nav_change_aud=nav_change.quantize(_CENT),
                              satellite_alpha_pp=alpha, headline=headline)


_BENCH_LABEL = {CORE: "55:15 SPY/INDA TR blend", XSMOM: "SPY TR",
                PEAD: "SPY TR", CASH: "0", TOTAL: "SPY TR"}


def render_monthly(m: MonthlyAttribution, shortfall: Attribution) -> str:
    """The docs/reports/attribution/YYYY-MM.md body. Every figure is a
    quantized Decimal formatted here for reading, never re-derived."""
    def pct(v: Decimal | None) -> str:
        return "n/a" if v is None else f"{v:+.2f}%"

    def pp(v: Decimal | None) -> str:
        return "n/a" if v is None else f"{v:+.2f} pp"

    def money(v: Decimal | None) -> str:
        return "n/a" if v is None else f"A${v:,.2f}"

    lines = [
        f"# Attribution — {m.period}",
        "",
        "Deterministic DCP computation (atlas/dcp/reporting/attribution.py; "
        "conventions in its module docstring). Core (beta) is graded against "
        "the signed ADR-0012 55:15 SPY/INDA total-return blend; the satellite "
        "sleeves and the total book against SPY total return (ADR-0009); "
        "cash against 0. Sleeve returns are flow-adjusted (a fill is capital "
        "moving, not performance). Contribution is the exact AUD "
        "decomposition of the month's NAV change — Brinson-lite, "
        "allocation-only (v1): by sleeve, no selection/interaction split.",
        "",
        f"## {m.headline}",
        "",
        "## Sleeves — month",
        "",
        "| sleeve | sessions | return | benchmark | excess | "
        "contribution | end value |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in m.sleeves:
        lines.append(
            f"| {s.sleeve} ({_BENCH_LABEL[s.sleeve]}) | {s.sessions} "
            f"| {pct(s.ret_pct)} | {pct(s.benchmark_pct)} "
            f"| {pp(s.excess_pp)} | {money(s.contribution_aud)} "
            f"| {money(s.end_value_aud)} |")
    total_contrib = sum((s.contribution_aud for s in m.sleeves
                         if s.sleeve != TOTAL), Decimal(0))
    lines += [
        "",
        f"Identity check (exact): sleeve contributions sum to "
        f"{money(total_contrib.quantize(_CENT))} = NAV change "
        f"{money(m.nav_change_aud)}.",
        "",
        "## Doc 04 §14 standing line — implementation shortfall",
        "",
        f"- entry: {shortfall.entry_shortfall.fills} fill(s), "
        f"avg {shortfall.entry_shortfall.avg_bps or 'n/a'} bps, "
        f"cost {money(shortfall.entry_shortfall.cost_aud)}",
        f"- exit: {shortfall.exit_shortfall.fills} fill(s), "
        f"avg {shortfall.exit_shortfall.avg_bps or 'n/a'} bps, "
        f"cost {money(shortfall.exit_shortfall.cost_aud)}",
        f"- realised P&L: {money(shortfall.realised_pnl_aud)} "
        f"({shortfall.lots_closed} lot(s) closed)",
        f"- LLM spend: ${shortfall.llm_spend_usd} USD (cost drag; "
        "never enters the AUD ledger)",
        "",
    ]
    return "\n".join(lines)


def generate_monthly_report(session: Session, clock: Clock, *, year: int,
                            month: int, reports_root: Path) -> Path:
    """Write docs/reports/attribution/YYYY-MM.md and append the audit event
    (append-only chain — every material action emits an event)."""
    m = compute_monthly(session, year=year, month=month)
    shortfall = compute_attribution(session, year=year, month=month)
    body = render_monthly(m, shortfall)
    out_dir = reports_root / "attribution"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{m.period}.md"
    path.write_text(body)
    PostgresAuditLog(session, clock).append(
        event_type=MONTHLY_REPORT_EVENT, entity_type="report",
        entity_id=m.period, actor_type="dcp", actor_id="attribution",
        payload={"period": m.period, "path": str(path),
                 "headline": m.headline,
                 "nav_change_aud": str(m.nav_change_aud),
                 "satellite_alpha_pp": (str(m.satellite_alpha_pp)
                                        if m.satellite_alpha_pp is not None
                                        else None),
                 "sleeves": {s.sleeve: {
                     "sessions": s.sessions,
                     "ret_pct": str(s.ret_pct) if s.ret_pct is not None else None,
                     "benchmark_pct": (str(s.benchmark_pct)
                                       if s.benchmark_pct is not None else None),
                     "contribution_aud": str(s.contribution_aud)}
                     for s in m.sleeves}})
    return path


# --------------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atlas attribution: daily series backfill + monthly report "
                    "(ADR-0012 consequence 4)")
    parser.add_argument("--month", help="generate docs/reports/attribution/"
                                        "YYYY-MM.md for this YYYY-MM period")
    parser.add_argument("--backfill", action="store_true",
                        help="compute the daily series from the earliest "
                             "snapshot forward (idempotent)")
    parser.add_argument("--now", help="ISO instant for deterministic re-runs "
                                      "(default: wall clock)")
    parser.add_argument("--reports-root", default=None,
                        help="override the docs/reports root (tests)")
    args = parser.parse_args()
    if not args.month and not args.backfill:
        parser.error("nothing to do: pass --month YYYY-MM and/or --backfill")
    clock: Clock = (FrozenClock(datetime.fromisoformat(args.now))
                    if args.now else SystemClock())
    root = (Path(args.reports_root) if args.reports_root
            else Path(__file__).resolve().parents[3] / "docs" / "reports")

    from atlas.core.db import session_scope
    with session_scope() as s:
        if args.backfill:
            reports = backfill_attribution(s, clock)
            for r in reports:
                print(f"{r.session}: {r.summary()}")
            print(f"backfilled {len(reports)} session(s)")
        if args.month:
            try:
                year, month = int(args.month[:4]), int(args.month[5:7])
                assert args.month == f"{year:04d}-{month:02d}"
            except (ValueError, AssertionError):
                parser.error(f"--month must be YYYY-MM, got {args.month!r}")
            path = generate_monthly_report(s, clock, year=year, month=month,
                                           reports_root=root)
            print(f"wrote {path}")


if __name__ == "__main__":
    main()
