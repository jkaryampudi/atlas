"""Production signal generation for the paper-approved PEAD/SUE recipe
(ADR-0013, pead-sue-tr) — the operational mirror of atlas/dcp/signals/xsmom/
generate.py.

quant.strategies holds the signed 'paper' row (pead-sue-tr /
foster_olsen_shevlin_sue v1.0.0); each rebalance this code writes the recipe's
verdicts into quant.signals so (a) the desk can cite them as evidence, (b) the
memo->proposal bridge can attach REAL signal UUIDs to proposals, and (c) the
band check + the bridge's sleeve budget can attribute the sleeve. Pure
deterministic compute plane: injectable Clock, vendor facts only, one audit
event per generation run, no agent import.

THE RECIPE, unchanged from the validated run (signals/pead/v1.py +
pead_pit_run.py): SUE_i = (epsActual - epsEstimate) / stdev(surprise over the
prior 8 reported quarters), >= 4 priors required; rank the live-signal universe
DESCENDING by SUE, ties alphabetical; winner set = winner_count(n_eligible) =
max(TOP_N, n_eligible // 10) — IMPORTED from xsmom_pit_run so the decile rule
can never drift from what momentum was approved on (both strategies are equal
weight over the same winner-decile construction; only the signal differs). EPS
is vendor backward-split-adjusted to the current basis and used DIRECTLY (no
on-read adjustment; signals/pead/v1.py SPLIT SAFETY).

UNIVERSE (ADR-0007 trading universe — the SAME set the momentum live signals
rank, NOT the backtest's point-in-time S&P 500): active instruments with
market='US' and instrument_type in ('stock','adr') — US single names and India
ADRs. India-sleeve ETFs, SPY and QQQ are 'etf' and excluded by construction.

ELIGIBILITY (mirrors pead_eligible in signals/pead/v1.py): a name is eligible
iff it has a vendor CLOSE on the signal session (tradable at t) AND a LIVE,
FRESH, DEFINED SUE at t — the most recent report knowable by t, within
STALENESS_SESSIONS (63) sessions, standardizable over >= 4 priors. A name
without one is not ranked (fail-closed: a series we cannot score honestly is a
series the ranker must not touch).

NO LOOK-AHEAD, structurally (what the adversarial audit will hammer):
- The signal session is the LATEST STORED US vendor session at or before the
  calendar's last completed session under the injected clock — the ranking
  never runs on a session whose close has not been ingested.
- The earnings query is capped at report_date <= signal session: a report
  ANNOUNCED after the signal session physically cannot enter the query.
- The panel calendar ENDS at the signal session, so build_earnings_view drops
  any report whose effective panel index is past t (an after-market print on
  the signal session itself is tradable only the next session).
- EarningsView.live(t) reads ONLY events with effective_index <= t, and a
  report's SUE depends only on STRICTLY-PRIOR reports (every one dated on or
  before that report's own report_date <= signal session). So flipping a FUTURE
  report's numbers leaves the ranking at t byte-identical — pinned by a
  structural test.

REBALANCE TRIGGER (monthly, identical semantics to xsmom.generate — the
calendar helpers is_month_end_session / next_rebalance_session are IMPORTED
from it so the two sleeves rebalance on the same session convention):
- month_end: the signal session is the LAST US session of its calendar month;
- initiation (one-time): the strategy has never produced a signal — initiate on
  the next cycle rather than idle to month-end (recorded trigger='initiation');
- catch_up: every stored signal has expired without a month-end run (the
  machine was down across a month boundary) — generate now at the CURRENT
  session rather than leave the sleeve unsignalled; a historical rebalance is
  never fabricated.
valid_until is the next month-end session STRICTLY AFTER the signal session.

Re-runs are idempotent: a session that already has rows for the strategy is
reported and skipped (no writes, no second audit event); the natural-key upsert
(ON CONFLICT DO NOTHING) backstops races.

The quant.signals.formation_return column (named for momentum's 12-1 return)
carries the ranked signal value for EVERY strategy; for PEAD that value is the
SUE — the column is a generic numeric, so no migration is needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Mapping
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.backtest.xsmom_pit_run import winner_count
from atlas.dcp.market_data.calendars import (
    last_completed_session,
    trading_days_between,
)
from atlas.dcp.market_data.earnings_history import EarningsSurprise
from atlas.dcp.signals.pead.v1 import build_earnings_view
from atlas.dcp.signals.xsmom.generate import (
    is_month_end_session,
    next_rebalance_session,
)

STRATEGY_FAMILY = "pead-sue-tr"          # the ADR-0013 signed row
VENDOR_SOURCE = "EodhdAdapter"           # same vendor discipline as the desk
UNIVERSE_TYPES = ("stock", "adr")        # US single names + India ADRs (ADR-0007)
SLEEVE_MAX_NAMES = 5                     # live-sleeve cap (Principal 2026-07-16):
                                         # top-5 by rank keeps each ~A$2,000 (§4 min)
VARIANT = "sue"                          # primary signal (surprise_pct is a cross-check)
# The panel calendar spans this many calendar days ending at the signal session.
# It must be comfortably longer than STALENESS_SESSIONS so that (a) every
# live-eligible report (report_date within ~63 sessions of t) lands on its exact
# panel index, and (b) any report older than the calendar maps to index 0 and is
# therefore correctly excluded as stale (t - 0 > 63). ~250 calendar days is
# ~170 US sessions, far past the 63-session staleness window.
_CAL_SLACK_DAYS = 250
SIGNAL_REF_PREFIX = "dcp:signal:pead:"
_TOP_NAMES_IN_AUDIT = 10                  # bounded audit payload


# ------------------------------------------------------------------- the rank

@dataclass(frozen=True)
class RankedPeadSignal:
    symbol: str
    rank: int                   # 1-based, descending SUE
    sue: float


def rank_pead_winners(sue_by_symbol: Mapping[str, float]) -> list[RankedPeadSignal]:
    """Winner set from {symbol: live SUE}: sort by (-sue, symbol) — the exact
    deterministic tie-break signals/pead/v1 uses — and keep
    winner_count(n_eligible) names (max(TOP_N, n_eligible // 10), imported from
    the approved run so momentum and PEAD share one decile rule)."""
    ranked = sorted(sue_by_symbol.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked[: winner_count(len(ranked))]
    return [RankedPeadSignal(symbol=s, rank=i + 1, sue=v)
            for i, (s, v) in enumerate(top)]


# ------------------------------------------------------------------- report

@dataclass(frozen=True)
class PeadSignalGenReport:
    reason: str                             # the cycle node's one-line result
    session: date | None = None
    trigger: str | None = None              # month_end | initiation | catch_up
    n_eligible: int = 0
    inserted: int = 0
    existing: int = 0                       # rows already present (idempotent)
    top: tuple[tuple[str, float], ...] = ()

    def summary(self) -> str:
        return self.reason


def _strategy_row(session: Session) -> tuple[UUID, str] | None:
    row = session.execute(text(
        "SELECT id, state FROM quant.strategies "
        "WHERE family = :f AND state IN ('paper','live') "
        "ORDER BY created_at DESC LIMIT 1"), {"f": STRATEGY_FAMILY}).first()
    return (row.id, str(row.state)) if row is not None else None


def _signal_session(session: Session, clock: Clock) -> date | None:
    """Latest stored US vendor session at or before the calendar's last
    completed session — the newest close the ranking may honestly read."""
    cutoff = last_completed_session("US", clock.now())
    latest: date | None = session.execute(text(
        "SELECT max(pb.bar_date) FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.market = 'US' AND pb.source = :src AND pb.bar_date <= :d"),
        {"src": VENDOR_SOURCE, "d": cutoff}).scalar()
    return latest


def _live_sues(session: Session, sig_session: date,
               ) -> tuple[dict[str, float], dict[str, UUID]]:
    """({symbol: live SUE at t}, {symbol: instrument_id}) over the ADR-0007
    trading universe at `sig_session` (module docstring rules; fail-closed per
    name). No look-ahead: the panel calendar ends at t and the earnings query
    is capped at report_date <= t."""
    cal = trading_days_between(
        "US", sig_session - timedelta(days=_CAL_SLACK_DAYS), sig_session)
    if not cal or cal[-1] != sig_session:
        return {}, {}                       # off-calendar or empty store
    t = len(cal) - 1                        # index of the signal session

    # tradable universe: a vendor close on the signal session (price at t)
    iids: dict[str, UUID] = {}
    for r in session.execute(text(
            "SELECT i.id, i.symbol FROM market.instruments i "
            "JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
            "WHERE i.is_active AND i.market = 'US' "
            "  AND i.instrument_type IN ('stock','adr') "
            "  AND pb.source = :src AND pb.close IS NOT NULL "
            "  AND pb.bar_date = :d "
            "ORDER BY i.symbol"),
            {"src": VENDOR_SOURCE, "d": sig_session}):
        iids[str(r.symbol)] = r.id
    if not iids:
        return {}, {}

    # completed earnings reports knowable by t (report_date <= t: a print
    # announced after the signal session physically cannot enter the ranking)
    reports: dict[str, list[EarningsSurprise]] = {}
    for r in session.execute(text(
            "SELECT i.symbol, es.fiscal_period_end, es.report_date, es.eps_actual, "
            "       es.eps_estimate, es.surprise_pct, es.before_after_market "
            "FROM market.earnings_surprises es "
            "JOIN market.instruments i ON i.id = es.instrument_id "
            "WHERE i.symbol = ANY(:syms) AND es.report_date <= :d "
            "ORDER BY i.symbol, es.fiscal_period_end"),
            {"syms": list(iids), "d": sig_session}):
        reports.setdefault(str(r.symbol), []).append(EarningsSurprise(
            symbol=str(r.symbol), fiscal_period_end=r.fiscal_period_end,
            report_date=r.report_date, eps_actual=Decimal(r.eps_actual),
            eps_estimate=Decimal(r.eps_estimate),
            surprise_pct=(Decimal(r.surprise_pct)
                          if r.surprise_pct is not None else None),
            before_after_market=r.before_after_market, currency=None))

    view = build_earnings_view(reports, cal)
    sues: dict[str, float] = {}
    for sym in iids:
        val = view.live(sym, t, variant=VARIANT)
        if val is not None:
            sues[sym] = val
    return sues, iids


def generate_pead_signals(session: Session, clock: Clock) -> PeadSignalGenReport:
    """One rebalance decision per call (module docstring semantics). Writes
    quant.signals rows and ONE quant.signals.generated audit event when a
    trigger fires; otherwise reports why it stayed idle. Never raises for an
    idle day — an exception here is a real failure the cycle should page."""
    strat = _strategy_row(session)
    if strat is None:
        return PeadSignalGenReport(
            reason=f"pead signals idle (no paper/live {STRATEGY_FAMILY} strategy)")
    strategy_id, state = strat

    sig_session = _signal_session(session, clock)
    if sig_session is None:
        return PeadSignalGenReport(
            reason="pead signals idle (no stored US vendor bars)")

    existing = int(session.execute(text(
        "SELECT count(*) FROM quant.signals "
        "WHERE strategy_id = :sid AND signal_date = :d"),
        {"sid": strategy_id, "d": sig_session}).scalar_one())
    if existing:
        return PeadSignalGenReport(
            reason=f"pead signals already generated for {sig_session} "
                   f"({existing} rows)",
            session=sig_session, existing=existing)

    has_any = session.execute(text(
        "SELECT 1 FROM quant.signals WHERE strategy_id = :sid LIMIT 1"),
        {"sid": strategy_id}).first() is not None
    live = session.execute(text(
        "SELECT 1 FROM quant.signals "
        "WHERE strategy_id = :sid AND valid_until >= :d LIMIT 1"),
        {"sid": strategy_id, "d": sig_session}).first() is not None

    if not has_any:
        trigger = "initiation"
    elif is_month_end_session(sig_session):
        trigger = "month_end"
    elif not live:
        trigger = "catch_up"
    else:
        return PeadSignalGenReport(
            reason=f"pead signals idle ({sig_session} is not a rebalance "
                   f"trigger; next {next_rebalance_session(sig_session)})",
            session=sig_session)

    sues, iids = _live_sues(session, sig_session)
    winners = rank_pead_winners(sues)
    valid_until = next_rebalance_session(sig_session)
    now = clock.now()

    inserted = 0
    for w in winners:
        got = session.execute(text(
            "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
            " direction, rank, formation_return, valid_until, created_at) "
            "VALUES (:sid, :iid, :d, 'long', :rank, :val, :vu, :ca) "
            "ON CONFLICT (strategy_id, instrument_id, signal_date) DO NOTHING "
            "RETURNING id"),
            {"sid": strategy_id, "iid": iids[w.symbol], "d": sig_session,
             "rank": w.rank, "val": Decimal(str(w.sue)),
             "vu": valid_until, "ca": now}).first()
        if got is not None:
            inserted += 1

    top = tuple((w.symbol, w.sue) for w in winners[:_TOP_NAMES_IN_AUDIT])
    PostgresAuditLog(session, clock).append(
        event_type="quant.signals.generated", entity_type="strategy",
        entity_id=f"{STRATEGY_FAMILY}/{sig_session.isoformat()}",
        actor_type="dcp", actor_id="pead_signal_generation",
        payload={"strategy_id": str(strategy_id), "family": STRATEGY_FAMILY,
                 "signal": "SUE (Foster-Olsen-Shevlin); PEAD",
                 "state": state, "trigger": trigger,
                 "session": sig_session.isoformat(),
                 "valid_until": valid_until.isoformat(),
                 "n_eligible": len(sues), "n_winners": len(winners),
                 "inserted": inserted,
                 "top": [{"symbol": s, "sue": round(v, 6)} for s, v in top]})
    return PeadSignalGenReport(
        reason=f"pead signals {trigger} @ {sig_session}: {inserted} of "
               f"{len(winners)} winners written (eligible {len(sues)}, "
               f"valid until {valid_until})",
        session=sig_session, trigger=trigger, n_eligible=len(sues),
        inserted=inserted, top=top)


# ------------------------------------------------------------ desk-side reads
# (agents may import these: pure SELECTs, no atlas.dcp.risk / dcp.execution)

def active_pead_signal_symbols(session: Session, clock: Clock) -> list[str]:
    """The desk's PEAD-sleeve PRIORITY lane: symbols with an ACTIVE PEAD signal
    (the paper/live pead-sue-tr strategy, signal_date <= last completed US
    session <= valid_until) and no non-expired trade proposal already standing
    for the instrument — those names are already in front of the Principal and
    do not need a fresh memo tonight. Rank order (strongest SUE first), deduped;
    the caller merges this with the momentum lane under the nightly budget."""
    now = clock.now()
    on = last_completed_session("US", now)
    rows = session.execute(text(
        "SELECT i.symbol FROM quant.signals s "
        "JOIN quant.strategies st ON st.id = s.strategy_id "
        "JOIN market.instruments i ON i.id = s.instrument_id "
        "WHERE st.family = :fam AND st.state IN ('paper','live') AND i.is_active "
        "  AND s.signal_date <= :on AND s.valid_until >= :on "
        "  AND NOT EXISTS (SELECT 1 FROM trading.trade_proposals tp "
        "                  WHERE tp.instrument_id = s.instrument_id "
        "                    AND tp.expires_at > :now) "
        # live-sleeve top-N cap (Principal 2026-07-16) — see xsmom generate.py
        "  AND s.rank <= :maxn "
        "ORDER BY s.rank, i.symbol"),
        {"fam": STRATEGY_FAMILY, "on": on, "now": now,
         "maxn": SLEEVE_MAX_NAMES}).all()
    return list(dict.fromkeys(str(r.symbol) for r in rows))


def extract_pead_signal_evidence(session: Session, symbol: str, *,
                                 on: date) -> tuple[str, str] | None:
    """The PEAD SIGNALS evidence block for build_evidence: (ref, body) for the
    newest PEAD signal on `symbol` active as of `on` from the paper/live
    pead-sue-tr strategy, or None (no fabricated line — same contract as the
    other extractors). Numeric / ISO-date / closed-vocabulary facts only; every
    number renders as a standalone token so a memo quoting it grounds verbatim.
    The ref embeds the REAL quant.signals UUID — the bridge resolves it into
    trade_proposals.signal_ids so the PEAD sleeve is attributable."""
    row = session.execute(text(
        "SELECT s.id, s.rank, s.formation_return, s.signal_date, s.valid_until, "
        "       st.family, st.state, "
        "       (SELECT count(*) FROM quant.signals w "
        "        WHERE w.strategy_id = s.strategy_id "
        "          AND w.signal_date = s.signal_date) AS n_winners "
        "FROM quant.signals s "
        "JOIN quant.strategies st ON st.id = s.strategy_id "
        "JOIN market.instruments i ON i.id = s.instrument_id "
        "WHERE i.symbol = :sym AND st.family = :fam "
        "  AND st.state IN ('paper','live') "
        "  AND s.signal_date <= :on AND s.valid_until >= :on "
        "ORDER BY s.signal_date DESC, s.rank LIMIT 1"),
        {"sym": symbol, "fam": STRATEGY_FAMILY, "on": on}).first()
    if row is None:
        return None
    sue = float(row.formation_return)
    ref = f"{SIGNAL_REF_PREFIX}{row.id}:{row.signal_date.isoformat()}"
    body = (f"Quant signal for {symbol} (strategy family {row.family}, state "
            f"{row.state} — approved for paper trading, ADR-0013): "
            f"earnings-surprise (SUE / PEAD) winner, rank {int(row.rank)} of "
            f"{int(row.n_winners)}, standardized unexpected earnings {sue:.4f}, "
            f"signal session {row.signal_date.isoformat()}, valid until "
            f"{row.valid_until.isoformat()}. Signal id {row.id}. The signal is "
            f"citable evidence for a BUY; sizing, pricing and execution remain "
            f"with the DCP and the risk engine.")
    return ref, body
