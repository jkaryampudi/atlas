"""Production signal generation for the paper-approved xsmom recipe (ADR-0010).

This module is the operational half of the approval: quant.strategies holds
the signed 'paper' row (xsmom-pit-tr / xsmom_pit v1.0.0), and each rebalance
this code writes the recipe's verdicts into quant.signals so (a) the desk can
cite them as evidence, (b) the memo->proposal bridge can attach REAL signal
UUIDs to proposals, and (c) the band check can attribute the sleeve. Pure
deterministic compute plane: injectable Clock, vendor bars only, one audit
event per generation run, no agent import.

THE RECIPE, unchanged from the validated run (atlas/dcp/signals/xsmom/v1.py +
xsmom_pit_run.py): 12-1 formation return close[t-SKIP]/close[t-LOOKBACK] - 1
on split-adjusted closes; rank descending, ties alphabetical; winner set =
winner_count(n_eligible) = max(TOP_N, n_eligible // 10) — IMPORTED from
xsmom_pit_run so the decile rule can never drift from what was approved.

UNIVERSE (documented gap, ADR-0010 caveat 3): the ADR-0007 trading universe
restricted to US single names — active instruments with market='US' and
instrument_type in ('stock','adr'). The recipe was validated on the
point-in-time S&P 500; the implementable-variant backtest is board item 5,
OPEN — paper results are the bridge evidence. India-sleeve ETFs and SPY are
'etf' and excluded by construction (SPY must never be ranked).

ELIGIBILITY (fail-closed, mirrors the panel's contiguity invariant): a name
is eligible iff it has a vendor close on EVERY of the last LOOKBACK+1 (=253)
US calendar sessions ending at the signal session — that proves >= SEASONING
prior sessions of stored data AND a price at t, exactly what v1's
eligible_symbols proves through the panel. A gap anywhere in the window
excludes the name for this rebalance (a series we cannot rank honestly is a
series the ranker must not touch).

NO LOOK-AHEAD, structurally: every query is capped at the signal session
(bar_date <= session, splits action_date <= session); a bar or split recorded
for a later date cannot reach the ranking. The signal session itself is the
LATEST STORED US vendor session at or before the calendar's last completed
session under the injected clock — the ranking never runs on a session whose
close has not been ingested.

REBALANCE TRIGGER (monthly, per the approved SPEC):
- month_end: the signal session is the LAST US session of its calendar month
  (exchange calendar decides, never day arithmetic);
- initiation (one-time, documented): if the strategy has never produced a
  signal, generate on the next cycle regardless — the paper book initiates
  after approval instead of idling to month-end. This is ONE extra rebalance
  relative to the backtest convention, recorded as trigger='initiation';
- catch_up (operational honesty): if every stored signal has expired
  (valid_until < session) without a month-end run — the machine was down
  across a month boundary — generate now rather than leave the sleeve
  unsignalled for a month. Ranks are computed at the CURRENT session; a
  historical rebalance is never fabricated.
valid_until is always the next month-end session STRICTLY AFTER the signal
session (= the next scheduled rebalance; for a mid-month initiation that is
the CURRENT month's last session).

Re-runs are idempotent: a session that already has rows for the strategy is
reported and skipped (no writes, no second audit event); the natural-key
upsert (ON CONFLICT DO NOTHING) backstops races.
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
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.calendars import (
    last_completed_session,
    next_trading_day,
    trading_days_between,
)
from atlas.dcp.market_data.models import Bar, Split
from atlas.dcp.signals.xsmom.v1 import LOOKBACK, SKIP

STRATEGY_FAMILY = "xsmom-pit-tr"        # the ADR-0010 signed row
VENDOR_SOURCE = "EodhdAdapter"          # same vendor-bar discipline as the desk
UNIVERSE_TYPES = ("stock", "adr")       # US single names (module docstring)
SIGNAL_STATES = ("paper", "live")       # states whose signals are operational
SLEEVE_MAX_NAMES = 5                    # live-sleeve cap (Principal 2026-07-16): a
                                        # 10% sleeve / decile is sub-min at A$100k
                                        # NAV, so trade only the top-5 by rank
WINDOW = LOOKBACK + 1                   # 253 sessions ending at t (contiguity)
_SKIP_IDX = WINDOW - 1 - SKIP           # index of t-SKIP inside the window
# ~550 calendar days always cover 253 US sessions (365 days ≈ 252 sessions)
_CAL_SLACK_DAYS = 550
SIGNAL_REF_PREFIX = "dcp:signal:xsmom:"
_TOP_NAMES_IN_AUDIT = 10                # bounded audit payload


# ------------------------------------------------------------------ calendar

def is_month_end_session(session_date: date) -> bool:
    """True iff `session_date`'s NEXT US session falls in a different month."""
    return next_trading_day("US", session_date).month != session_date.month


def next_rebalance_session(after: date) -> date:
    """The next month-end US session STRICTLY AFTER `after` — the sleeve's
    next scheduled rebalance (= valid_until for signals formed at `after`)."""
    d = next_trading_day("US", after)
    while not is_month_end_session(d):
        d = next_trading_day("US", d)
    return d


# ------------------------------------------------------------------ the rank

@dataclass(frozen=True)
class RankedSignal:
    symbol: str
    rank: int                   # 1-based, descending formation return
    formation_return: float


def rank_winners(formation: Mapping[str, float]) -> list[RankedSignal]:
    """Winner set from {symbol: formation return}: sort by (-return, symbol)
    — v1's exact deterministic tie-break — and keep winner_count(n_eligible)
    names (max(TOP_N, n_eligible // 10), imported from the approved run)."""
    ranked = sorted(formation.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked[: winner_count(len(ranked))]
    return [RankedSignal(symbol=s, rank=i + 1, formation_return=r)
            for i, (s, r) in enumerate(top)]


# ------------------------------------------------------------------- report

@dataclass(frozen=True)
class SignalGenReport:
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


def _formation_returns(session: Session, sig_session: date,
                       ) -> tuple[dict[str, float], dict[str, UUID]]:
    """({symbol: formation return}, {symbol: instrument_id}) over the eligible
    universe at `sig_session` (module docstring rules; fail-closed per name)."""
    cal = trading_days_between(
        "US", sig_session - timedelta(days=_CAL_SLACK_DAYS), sig_session)
    if len(cal) < WINDOW or cal[-1] != sig_session:
        return {}, {}                       # off-calendar or too-young store
    window = cal[-WINDOW:]
    probe = (window[0], window[_SKIP_IDX], window[-1])   # t-252, t-21, t

    counts: dict[str, int] = {}
    closes: dict[str, dict[date, Decimal]] = {}
    iids: dict[str, UUID] = {}
    for r in session.execute(text(
            "SELECT i.id, i.symbol, pb.bar_date, pb.close "
            "FROM market.instruments i "
            "JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
            "WHERE i.is_active AND i.market = 'US' "
            "  AND i.instrument_type IN ('stock','adr') "
            "  AND pb.source = :src AND pb.close IS NOT NULL "
            "  AND pb.bar_date = ANY(:dates) "
            "ORDER BY i.symbol, pb.bar_date"),
            {"src": VENDOR_SOURCE, "dates": list(window)}):
        sym = str(r.symbol)
        iids[sym] = r.id
        counts[sym] = counts.get(sym, 0) + 1
        if r.bar_date in probe:
            closes.setdefault(sym, {})[r.bar_date] = Decimal(r.close)

    splits: dict[str, list[Split]] = {}
    for r in session.execute(text(
            "SELECT i.symbol, ca.action_date, ca.ratio "
            "FROM market.corporate_actions ca "
            "JOIN market.instruments i ON i.id = ca.instrument_id "
            "WHERE i.is_active AND i.market = 'US' "
            "  AND i.instrument_type IN ('stock','adr') "
            "  AND ca.action_type = 'split' AND ca.action_date <= :d "
            "ORDER BY i.symbol, ca.action_date"), {"d": sig_session}):
        splits.setdefault(str(r.symbol), []).append(Split(
            symbol=str(r.symbol), action_date=r.action_date,
            ratio=Decimal(r.ratio)))

    formation: dict[str, float] = {}
    for sym, n in counts.items():
        if n < WINDOW or len(closes.get(sym, {})) != len(probe):
            continue                        # gap in the window: fail closed
        by_date = closes[sym]
        bars = [Bar(symbol=sym, bar_date=d, open=by_date[d], high=by_date[d],
                    low=by_date[d], close=by_date[d], volume=0) for d in probe]
        adj = adjust_for_splits(bars, splits.get(sym, []))
        c_form, c_skip = float(adj[0].close), float(adj[1].close)
        if c_form <= 0:
            continue                        # unpriceable base: fail closed
        formation[sym] = c_skip / c_form - 1.0
    return formation, iids


def generate_signals(session: Session, clock: Clock) -> SignalGenReport:
    """One rebalance decision per call (module docstring semantics). Writes
    quant.signals rows and ONE quant.signals.generated audit event when a
    trigger fires; otherwise reports why it stayed idle. Never raises for an
    idle day — an exception here is a real failure the cycle should page."""
    strat = _strategy_row(session)
    if strat is None:
        return SignalGenReport(
            reason=f"signals idle (no paper/live {STRATEGY_FAMILY} strategy)")
    strategy_id, state = strat

    sig_session = _signal_session(session, clock)
    if sig_session is None:
        return SignalGenReport(reason="signals idle (no stored US vendor bars)")

    existing = int(session.execute(text(
        "SELECT count(*) FROM quant.signals "
        "WHERE strategy_id = :sid AND signal_date = :d"),
        {"sid": strategy_id, "d": sig_session}).scalar_one())
    if existing:
        return SignalGenReport(
            reason=f"signals already generated for {sig_session} "
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
        return SignalGenReport(
            reason=f"signals idle ({sig_session} is not a rebalance trigger; "
                   f"next {next_rebalance_session(sig_session)})",
            session=sig_session)

    formation, iids = _formation_returns(session, sig_session)
    winners = rank_winners(formation)
    valid_until = next_rebalance_session(sig_session)
    now = clock.now()

    inserted = 0
    for w in winners:
        got = session.execute(text(
            "INSERT INTO quant.signals (strategy_id, instrument_id, signal_date, "
            " direction, rank, formation_return, valid_until, created_at) "
            "VALUES (:sid, :iid, :d, 'long', :rank, :ret, :vu, :ca) "
            "ON CONFLICT (strategy_id, instrument_id, signal_date) DO NOTHING "
            "RETURNING id"),
            {"sid": strategy_id, "iid": iids[w.symbol], "d": sig_session,
             "rank": w.rank, "ret": Decimal(str(w.formation_return)),
             "vu": valid_until, "ca": now}).first()
        if got is not None:
            inserted += 1

    top = tuple((w.symbol, w.formation_return)
                for w in winners[:_TOP_NAMES_IN_AUDIT])
    PostgresAuditLog(session, clock).append(
        event_type="quant.signals.generated", entity_type="strategy",
        entity_id=f"{STRATEGY_FAMILY}/{sig_session.isoformat()}",
        actor_type="dcp", actor_id="xsmom_signal_generation",
        payload={"strategy_id": str(strategy_id), "family": STRATEGY_FAMILY,
                 "state": state, "trigger": trigger,
                 "session": sig_session.isoformat(),
                 "valid_until": valid_until.isoformat(),
                 "n_eligible": len(formation), "n_winners": len(winners),
                 "inserted": inserted,
                 "top": [{"symbol": s, "formation_return": round(r, 6)}
                         for s, r in top]})
    return SignalGenReport(
        reason=f"signals {trigger} @ {sig_session}: {inserted} of "
               f"{len(winners)} winners written (eligible {len(formation)}, "
               f"valid until {valid_until})",
        session=sig_session, trigger=trigger, n_eligible=len(formation),
        inserted=inserted, top=top)


# ------------------------------------------------------------ desk-side reads
# (agents may import these: pure SELECTs, no atlas.dcp.risk / dcp.execution)

def active_signal_symbols(session: Session, clock: Clock) -> list[str]:
    """The desk's PRIORITY lane: symbols with an ACTIVE signal (paper/live
    strategy, signal_date <= last completed US session <= valid_until) and no
    non-expired trade proposal already standing for the instrument — those
    names are already in front of the Principal and do not need a fresh memo
    tonight. Rank order (strongest first), deduped; the caller prepends this
    to the scanner shortlist under the unchanged nightly budget."""
    now = clock.now()
    on = last_completed_session("US", now)
    rows = session.execute(text(
        "SELECT i.symbol FROM quant.signals s "
        "JOIN quant.strategies st ON st.id = s.strategy_id "
        "JOIN market.instruments i ON i.id = s.instrument_id "
        "WHERE st.state IN ('paper','live') AND i.is_active "
        "  AND s.signal_date <= :on AND s.valid_until >= :on "
        "  AND NOT EXISTS (SELECT 1 FROM trading.trade_proposals tp "
        "                  WHERE tp.instrument_id = s.instrument_id "
        "                    AND tp.expires_at > :now) "
        # ADR-0014 + §4 MIN_POSITION_AUD at the current NAV: a 10% sleeve split
        # across the full winner decile is ~A$1,000/name, below the A$2,000
        # minimum, so the LIVE sleeve trades only its top-SLEEVE_MAX_NAMES by
        # rank (rank is per-strategy). The strategy stays validated on the full
        # decile; this caps only what deploys. (Principal decision 2026-07-16.)
        "  AND s.rank <= :maxn "
        "ORDER BY s.rank, i.symbol"),
        {"on": on, "now": now, "maxn": SLEEVE_MAX_NAMES}).all()
    return list(dict.fromkeys(str(r.symbol) for r in rows))


def extract_signal_evidence(session: Session, symbol: str, *,
                            on: date) -> tuple[str, str] | None:
    """The SIGNALS evidence block for build_evidence: (ref, body) for the
    newest signal on `symbol` that is active as of `on` from a paper/live
    strategy, or None (no fabricated line — same contract as the other
    extractors). Numeric / ISO-date / closed-vocabulary facts only: family is
    a reviewed constant on the strategy row, state is CHECK-limited, every
    number renders as a standalone token so a memo quoting it grounds
    verbatim. The ref embeds the REAL quant.signals UUID — the bridge
    resolves it into trade_proposals.signal_ids (no more synthetic uuid5 for
    signal-backed memos)."""
    row = session.execute(text(
        "SELECT s.id, s.rank, s.formation_return, s.signal_date, s.valid_until, "
        "       st.family, st.state, "
        "       (SELECT count(*) FROM quant.signals w "
        "        WHERE w.strategy_id = s.strategy_id "
        "          AND w.signal_date = s.signal_date) AS n_winners "
        "FROM quant.signals s "
        "JOIN quant.strategies st ON st.id = s.strategy_id "
        "JOIN market.instruments i ON i.id = s.instrument_id "
        "WHERE i.symbol = :sym AND st.state IN ('paper','live') "
        "  AND s.signal_date <= :on AND s.valid_until >= :on "
        "ORDER BY s.signal_date DESC, s.rank LIMIT 1"),
        {"sym": symbol, "on": on}).first()
    if row is None:
        return None
    ret_pct = float(row.formation_return) * 100
    ref = (f"{SIGNAL_REF_PREFIX}{row.id}:{row.signal_date.isoformat()}")
    body = (f"Quant signal for {symbol} (strategy family {row.family}, state "
            f"{row.state} — approved for paper trading, ADR-0010): "
            f"cross-sectional 12-1 momentum winner, rank {int(row.rank)} of "
            f"{int(row.n_winners)}, formation return {ret_pct:.2f} percent, "
            f"signal session {row.signal_date.isoformat()}, valid until "
            f"{row.valid_until.isoformat()}. Signal id {row.id}. The signal "
            f"is citable evidence for a BUY; sizing, pricing and execution "
            f"remain with the DCP and the risk engine.")
    return ref, body
