"""Deterministic universe scanner v1 (ADR-0007): the top of the opportunity funnel.

ATTENTION, NOT PREDICTION — read this before tuning anything here. The
scanner's only job is to decide where the expensive LLM desk LOOKS each
cycle: it sweeps the full active universe for free (pure SQL + arithmetic,
no LLM, no network) and routes a small shortlist onward. Its rules are an
attention heuristic, explicitly NON-PREDICTIVE: v1 makes NO alpha claim and
nothing here has been validated by the backtest gates. Per ADR-0007's
consequences, scanner rules are STRATEGY SURFACE — every component and
threshold below is an implicit trial that backtesting must eventually
validate under the trial-registry / deflated-Sharpe discipline (ADR-0002).
Until then the scanner may only decide desk attention, never sizing,
pricing, or execution (those stay behind the risk engine and human
approval; CLAUDE.md invariant 2). This module is distinct from the P2 LLM
"scanner" agent role: this one is deterministic compute plane.

Criteria v1 (CRITERIA_VERSION pins these in every audit event):

- ELIGIBLE: ACTIVE instruments with a vendor bar on the last completed
  session for their market AND >= LOOKBACK_SESSIONS stored sessions of
  history. Fail-closed: thin or stale series are ineligible and counted in
  the report with a reason — a series we cannot rank honestly is a series
  the ranker must not touch.
- SCORE: rank_z(|RETURN_SESSIONS-session return|) + rank_z(volume surge),
  where volume surge = mean(volume, last SURGE_SESSIONS sessions) /
  mean(volume, last LOOKBACK_SESSIONS sessions) and rank_z is the
  cross-sectional rank scaled to [0, 1] per component. Both components ask
  "is something HAPPENING here?" (movement in either direction, unusual
  volume), never "will it go up?".
- SHORTLIST: top_n by score, PLUS — never consuming a top_n slot — every
  symbol with an open position, a live proposal (risk_review /
  pending_approval / approved) or a live order (pending_submit / submitted /
  partially_filled): the desk must never lose sight of what the book
  already holds, however boring its tape.

No look-ahead, structurally: only bars dated at or before each market's
last completed session (calendars.last_completed_session under the injected
clock) are ever read. Determinism: same stored bars + same clock => the
same report, byte for byte — every ordering is keyed, and every tie breaks
by symbol. Floats are fine here (attention scores are not ledger money);
ORDER is what must be exact, not the tenth decimal.

Documented resolutions (v1, deliberate):
- Vendor-bar discipline: only source = 'EodhdAdapter' bars count, the same
  filter desk_symbols / build_evidence / the marking path apply — a symbol
  shortlisted on non-vendor bars would reach a desk that cannot evidence it.
- rank_z ties take distinct adjacent ranks in symbol order (rank i/(n-1) on
  the (value, symbol) sort) rather than averaged ranks: cheaper, total, and
  exactly as deterministic. A single-name cross-section ranks 0.0.
- A dead-volume series (60-session mean volume = 0) gets surge 0.0: no
  tape, no attention. A non-positive close 20 sessions back is ineligible
  (fail-closed) — a nonsense base must never rank, high or low.
- One ineligibility reason per symbol, first failing check in a fixed order
  (no calendar -> no bars -> stale -> thin -> unpriceable).
- Held/in-flight names are shortlisted even when INELIGIBLE (components
  None) and even when the instrument is no longer active: the book outranks
  every filter. Eligible held names carry their real score components but
  never occupy a top_n slot.
- The audit payload stays bounded: shortlist rows (top_n + the held book,
  itself capped by L1 at ~8-12 names) and COUNTS — never all ~112 score rows.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.market_data.calendars import last_completed_session

CRITERIA_VERSION = "1.0"
LOOKBACK_SESSIONS = 60  # stored history required; volume-surge baseline window
RETURN_SESSIONS = 20    # attention component 1: |20-session return|
SURGE_SESSIONS = 5      # attention component 2: recent-volume window
VENDOR_SOURCE = "EodhdAdapter"  # same vendor-bar discipline as desk_symbols/_latest_close

# a live claim on the book (Doc 05 §5): the desk must keep watching these
LIVE_PROPOSAL_STATES = ("risk_review", "pending_approval", "approved")
LIVE_ORDER_STATES = ("pending_submit", "submitted", "partially_filled")

_BARS_SQL = f"""
SELECT symbol, bar_date, close, volume FROM (
  SELECT i.symbol, pb.bar_date, pb.close, COALESCE(pb.volume, 0) AS volume,
         row_number() OVER (PARTITION BY i.id ORDER BY pb.bar_date DESC) AS rn
  FROM market.instruments i
  JOIN market.price_bars_daily pb ON pb.instrument_id = i.id
  WHERE i.is_active AND i.market = :m AND pb.source = '{VENDOR_SOURCE}'
    AND pb.close IS NOT NULL AND pb.bar_date <= :cutoff
) w WHERE rn <= :lookback ORDER BY symbol, bar_date
"""

_HELD_SQL = f"""
SELECT i.symbol FROM trading.positions p
  JOIN market.instruments i ON i.id = p.instrument_id
  WHERE p.closed_at IS NULL
UNION
SELECT i.symbol FROM trading.trade_proposals tp
  JOIN market.instruments i ON i.id = tp.instrument_id
  WHERE tp.state IN ({",".join(f"'{s}'" for s in LIVE_PROPOSAL_STATES)})
UNION
SELECT i.symbol FROM trading.orders o
  JOIN trading.trade_proposals tp ON tp.id = o.proposal_id
  JOIN market.instruments i ON i.id = tp.instrument_id
  WHERE o.state IN ({",".join(f"'{s}'" for s in LIVE_ORDER_STATES)})
"""


@dataclass(frozen=True)
class SymbolScore:
    """One symbol's attention components. Ranks are cross-sectional [0, 1]."""
    symbol: str
    ret20_abs: float
    ret20_rank: float
    volume_surge: float
    surge_rank: float
    score: float  # ret20_rank + surge_rank, in [0, 2]


@dataclass(frozen=True)
class ShortlistEntry:
    symbol: str
    held: bool  # open position / live proposal / live order — no top_n slot
    components: SymbolScore | None  # None: held name outside today's eligible set


@dataclass(frozen=True)
class ScanReport:
    criteria_version: str
    sessions: tuple[tuple[str, date], ...]  # (market, last completed session)
    top_n: int
    scanned: int  # every ACTIVE instrument
    ineligible: tuple[tuple[str, str], ...]  # (symbol, reason), symbol-ordered
    shortlist: tuple[ShortlistEntry, ...]  # scored (rank order) then held (symbol order)

    @property
    def eligible(self) -> int:
        return self.scanned - len(self.ineligible)

    @property
    def n_held(self) -> int:
        return sum(1 for e in self.shortlist if e.held)

    @property
    def n_scored(self) -> int:
        return len(self.shortlist) - self.n_held

    def summary(self) -> str:
        return f"scanned {self.scanned} · shortlist {self.n_scored}+{self.n_held} held"


def volume_surge(volumes: Sequence[int]) -> float:
    """mean(last SURGE_SESSIONS) / mean(last LOOKBACK_SESSIONS); 0.0 when the
    baseline mean is zero (a dead tape draws no attention). Requires the full
    LOOKBACK_SESSIONS window — a shorter series must fail eligibility, never
    rank on a quietly different denominator."""
    if len(volumes) < LOOKBACK_SESSIONS:
        raise ValueError(f"volume_surge needs >= {LOOKBACK_SESSIONS} volumes, "
                         f"got {len(volumes)}")
    base = volumes[-LOOKBACK_SESSIONS:]
    base_mean = sum(base) / len(base)
    if base_mean <= 0:
        return 0.0
    recent = volumes[-SURGE_SESSIONS:]
    return (sum(recent) / len(recent)) / base_mean


def rank01(values: Mapping[str, float]) -> dict[str, float]:
    """Cross-sectional rank scaled to [0, 1]: sort by (value, symbol), rank
    i/(n-1). Ties take distinct adjacent ranks in symbol order (deterministic
    by construction); a single-name cross-section ranks 0.0."""
    ordered = sorted(values, key=lambda sym: (values[sym], sym))
    denom = max(len(ordered) - 1, 1)
    return {sym: i / denom for i, sym in enumerate(ordered)}


def score_cross_section(
        series: Mapping[str, tuple[Sequence[float], Sequence[int]]],
) -> tuple[SymbolScore, ...]:
    """Score {symbol: (closes, volumes)} — both ascending, closes >=
    RETURN_SESSIONS+1 with a positive base close, volumes >= LOOKBACK_SESSIONS
    (scan()'s eligibility gate guarantees this; violations raise, fail-closed).
    Returns scores sorted by (-score, symbol): deterministic for any input order."""
    rets: dict[str, float] = {}
    surges: dict[str, float] = {}
    for sym, (closes, volumes) in series.items():
        if len(closes) < RETURN_SESSIONS + 1:
            raise ValueError(f"{sym}: needs >= {RETURN_SESSIONS + 1} closes, "
                             f"got {len(closes)}")
        base = closes[-(RETURN_SESSIONS + 1)]
        if base <= 0:
            raise ValueError(f"{sym}: non-positive close {RETURN_SESSIONS} sessions back")
        rets[sym] = abs(closes[-1] / base - 1.0)
        surges[sym] = volume_surge(volumes)
    ret_rank = rank01(rets)
    surge_rank = rank01(surges)
    scored = [SymbolScore(symbol=sym, ret20_abs=rets[sym], ret20_rank=ret_rank[sym],
                          volume_surge=surges[sym], surge_rank=surge_rank[sym],
                          score=ret_rank[sym] + surge_rank[sym])
              for sym in series]
    return tuple(sorted(scored, key=lambda sc: (-sc.score, sc.symbol)))


def _held_symbols(session: Session) -> frozenset[str]:
    return frozenset(r.symbol for r in session.execute(text(_HELD_SQL)).all())


def _entry_payload(e: ShortlistEntry) -> dict[str, Any]:
    c = e.components
    return {"symbol": e.symbol, "held": e.held,
            "score": round(c.score, 6) if c is not None else None,
            "ret20_abs": round(c.ret20_abs, 6) if c is not None else None,
            "ret20_rank": round(c.ret20_rank, 6) if c is not None else None,
            "volume_surge": round(c.volume_surge, 6) if c is not None else None,
            "surge_rank": round(c.surge_rank, 6) if c is not None else None}


def scan(session: Session, clock: Clock, *, top_n: int = 5) -> ScanReport:
    """Sweep the full active universe; return the attention shortlist for the
    desk. Appends exactly one scanner.completed audit event (a scan is a
    material routing decision: it decides what the LLM desk never sees)."""
    if top_n < 0:
        raise ValueError(f"top_n must be >= 0, got {top_n}")
    now = clock.now()
    instruments = session.execute(text(
        "SELECT symbol, market FROM market.instruments WHERE is_active "
        "ORDER BY symbol")).all()

    sessions: dict[str, date] = {}
    no_calendar: set[str] = set()
    for m in sorted({r.market for r in instruments}):
        try:
            sessions[m] = last_completed_session(m, now)
        except ValueError:
            no_calendar.add(m)  # fail-closed below: the whole market is ineligible

    # last LOOKBACK_SESSIONS vendor bars per instrument, hard-capped at each
    # market's last completed session — the structural no-look-ahead guarantee
    hist: dict[str, list[tuple[float, int]]] = {}
    latest: dict[str, date] = {}
    for m, cutoff in sorted(sessions.items()):
        for r in session.execute(text(_BARS_SQL), {"m": m, "cutoff": cutoff,
                                                   "lookback": LOOKBACK_SESSIONS}):
            hist.setdefault(r.symbol, []).append((float(r.close), int(r.volume)))
            latest[r.symbol] = r.bar_date  # rows ascend, last write is newest

    ineligible: list[tuple[str, str]] = []
    series: dict[str, tuple[list[float], list[int]]] = {}
    for r in instruments:
        sym: str = r.symbol
        if r.market in no_calendar:
            ineligible.append((sym, f"no exchange calendar for market {r.market!r}"))
            continue
        cutoff = sessions[r.market]
        rows = hist.get(sym)
        if not rows:
            ineligible.append((sym, f"no stored vendor bars on or before {cutoff}"))
            continue
        if latest[sym] != cutoff:
            ineligible.append((sym, f"stale: latest bar {latest[sym]} "
                                    f"< last session {cutoff}"))
            continue
        if len(rows) < LOOKBACK_SESSIONS:
            ineligible.append((sym, f"thin history: {len(rows)} "
                                    f"< {LOOKBACK_SESSIONS} stored sessions"))
            continue
        closes = [c for c, _ in rows]
        if closes[-(RETURN_SESSIONS + 1)] <= 0:
            ineligible.append((sym, f"unpriceable: non-positive close "
                                    f"{RETURN_SESSIONS} sessions back"))
            continue
        series[sym] = (closes, [v for _, v in rows])

    scores = score_cross_section(series)
    by_symbol = {sc.symbol: sc for sc in scores}
    held = _held_symbols(session)
    top = [sc for sc in scores if sc.symbol not in held][:top_n]
    shortlist = tuple(
        [ShortlistEntry(symbol=sc.symbol, held=False, components=sc) for sc in top]
        + [ShortlistEntry(symbol=sym, held=True, components=by_symbol.get(sym))
           for sym in sorted(held)])

    report = ScanReport(criteria_version=CRITERIA_VERSION,
                        sessions=tuple(sorted(sessions.items())), top_n=top_n,
                        scanned=len(instruments), ineligible=tuple(ineligible),
                        shortlist=shortlist)
    PostgresAuditLog(session, clock).append(
        event_type="scanner.completed", entity_type="scanner",
        entity_id=now.date().isoformat(), actor_type="dcp", actor_id="scanner_v1",
        payload={"criteria_version": CRITERIA_VERSION, "top_n": top_n,
                 "sessions": {m: d.isoformat() for m, d in report.sessions},
                 "scanned": report.scanned, "eligible": report.eligible,
                 "ineligible": len(report.ineligible),
                 "shortlist": [_entry_payload(e) for e in report.shortlist]})
    return report
