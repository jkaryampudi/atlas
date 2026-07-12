"""Memo scorecard (deterministic compute plane): the desk graded on its own
record. Every committee memo's recommendation is tracked against what the
instrument actually did afterward, SPY-relative, into the append-only
research.memo_outcomes table (migration 0016).

SCORING SEMANTICS — read this before interpreting any number here:

- The benchmark is RELATIVE, never absolute (ADR-0009: buy-and-hold SPY is
  the fund's honest alternative). excess = instrument return - SPY return
  over the SAME anchor->forward dates.
- A BUY is VINDICATED when excess > 0 at the horizon: the desk picked a
  market-beater.
- A REJECT is VINDICATED when excess < 0: the desk dodged an underperformer.
  Dodging a stock that ROSE but rose less than the market is a CORRECT
  rejection — the capital would have lagged the passive core.
- A dead heat (excess == 0 at the 6dp quantum) vindicates neither direction.
- HOLD (and every other non-directional recommendation) and SHADOW-run memos
  are tracked in rows like everything else — the record is complete — but
  EXCLUDED from vindication rates: no direction to grade / non-actionable
  output (ADR-0005 pattern 4). vindicated() returns None for them.

DARTBOARD BASE RATES (desk-review 2026-07 item 5): a vindication rate alone
cannot support a skill claim, because the base rate is free. The honest
comparator is a direction-blind dart thrown at the same tracked outcomes:
REJECT's baseline is the fraction of ALL tracked outcomes at the horizon with
excess < 0, BUY's the fraction with excess > 0 (dead heats count for neither
— exactly the vindication rule). The point of publishing rate-minus-baseline:
an always-REJECT desk in a falling market grades near-perfect on raw
vindication while the dart grades the same — its EDGE is zero, and zero edge
is the verdict. dartboard_baseline() is the one place this rule lives.

DISSENT GRADING (same review item): every directional committee memo records
a dissent — the strongest case AGAINST the call — so the dissent's verdict is
derivable at read time as the EXACT complement of vindicated(): dissent right
= NOT vindicated for BUY/REJECT. A dead heat therefore grades the dissent
right (the call failed to beat the passive core — conservative against the
desk by construction). HOLD and shadow memos stay ungraded on both sides
(None): a memo with no gradable direction has no gradable dissent. No schema
change — dissent_right() is a pure read over the recorded excess.

MECHANICS:

- Anchor: the last vendor bar_date <= the memo's created_at UTC date — the
  session the memo's evidence ended on. A memo dated on a non-session day
  anchors to the prior session's bar.
- Horizons: exactly 20 and 60 SESSIONS after the anchor in the instrument's
  OWN priceable session sequence (vendor bars with a non-NULL close), never
  calendar days. No bar that far out yet => the outcome is immature, skipped
  with a reason, retried next cycle.
- fwd_return = fwd_close/anchor_close - 1; spy_return over the same
  anchor->fwd dates from SPY closes; excess = fwd_return - spy_return. All
  three are quantized to the 6dp column quantum (fwd/spy first, so excess is
  exact). Fail-closed skips: SPY missing either exact date, no resolvable
  instrument, no anchor bar, non-positive close at either end.
- INSTRUMENT RESOLUTION (desk-review 2026-07 item 5): exactly one ACTIVE
  instrument for the symbol wins — the bridge's rule. With no active row,
  exactly one row of ANY activity resolves too: grading needs bars, not
  tradability. Analyze-box memos land on analysis-only instruments
  (is_active=FALSE, ops/analyze.py) and were permanently ungradeable under
  the active-only rule — corrupted-by-omission, per the review. Ambiguity
  (two active rows, or no active and several inactive) still fails closed.
- ANALYSIS-ONLY BAR TOP-UP (same review item): nightly ingest skips inactive
  instruments, so an analysis-only memo's forward bars never arrive and its
  outcomes never mature. When the ops layer passes `adapter_for` (a
  (symbol, exchange) -> MarketDataAdapter factory; the t9 call site passes
  vendor_adapter_for), the scorecard tops the bars up itself, BOUNDED: only
  inactive instruments, only symbols with committee memos still awaiting an
  outcome row, only the missing session window (incremental from the latest
  stored vendor bar through the last completed session at the injected
  clock — never a partial or forward bar), splits recorded alongside so the
  split-adjusted read stays honest. No stored vendor bars at all = no
  top-up: this is maintenance of a window ops/analyze.py already fetched,
  never a backfill. Vendor failures are fail-soft per symbol (noted in the
  report; grading proceeds on stored bars). Default None = pure read —
  deterministic replays and tests stay hermetic unless ops opts in.
- SPLIT-ADJUSTED, both legs (desk-review 2026-07 item 2): vendor bars are
  stored RAW, so a split between anchor and forward would fabricate a phantom
  outcome — a 10:1 split reads as a -90% "return" — and write it into the
  append-only memo_outcomes forever. Every series (the instrument's AND
  SPY's) is passed through the property-tested adjuster
  (market_data/adjustment.py) before anchoring; only splits with action_date
  on or before the clock's date apply (no look-ahead — a replay of an old
  date computes what that date could have known). anchor_close/fwd_close are
  therefore stored in split-adjusted (post-split) terms; returns are exact
  either way when no split intervenes, and honest when one does.
- No look-ahead, structurally (CLAUDE.md invariant 8): only bars dated at or
  before the injected clock's UTC date are ever read, so a deterministic
  replay of an old date computes exactly what that date could have known.
- Idempotent: UNIQUE (memo_id, horizon_sessions) + insert-if-absent; rows
  already recorded are counted (`already`), never rewritten — an outcome,
  once matured and recorded, is a fact. ON CONFLICT DO NOTHING is the
  belt-and-braces under concurrency (first writer wins).
- ONE research.scorecard.updated audit event per run WHEN new rows were
  written (count + memo ids); none when zero — a no-op night leaves no
  ledger noise.

Two-plane wall: this module is pure DCP — deterministic math over recorded
tables, no agent imports, injectable Clock only. Floats never appear; ledger
numerics stay Decimal end to end.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.core.config import get_settings
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter, vendor_symbol
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.daily import incremental_sessions
from atlas.dcp.market_data.ingest import record_split, upsert_bar
from atlas.dcp.market_data.models import Bar, Split

_REPO = Path(__file__).resolve().parents[2]

HORIZONS: tuple[int, ...] = (20, 60)   # sessions; must match the 0016 CHECK
BENCHMARK_SYMBOL = "SPY"               # ADR-0009: the fund's honest alternative
VENDOR_SOURCE = "EodhdAdapter"         # same vendor-bar discipline as scanner/bridge
_RETURN_QUANT = Decimal("0.000001")    # memo_outcomes numeric(12,6) quantum


@dataclass(frozen=True)
class MemoRef:
    """One committee memo as the planner sees it."""
    memo_id: str
    symbol: str | None
    recommendation: str | None
    shadow: bool
    memo_date: date                    # created_at in UTC, date part


@dataclass(frozen=True)
class PlannedOutcome:
    """One matured (memo, horizon) row ready to insert."""
    memo_id: str
    symbol: str
    recommendation: str | None
    shadow: bool
    horizon_sessions: int
    anchor_date: date
    anchor_close: Decimal
    fwd_date: date                     # for the report/SPY lookup; not a column
    fwd_close: Decimal
    fwd_return: Decimal
    spy_return: Decimal
    excess: Decimal


@dataclass(frozen=True)
class OutcomeSkip:
    """A counted, reasoned non-write. horizon_sessions None = memo-level
    (the skip covers every horizon at once)."""
    memo_id: str
    symbol: str | None
    horizon_sessions: int | None
    reason: str                        # stable prefix: immature | no instrument | missing bars


@dataclass(frozen=True)
class ScorecardReport:
    written: tuple[PlannedOutcome, ...]
    skipped: tuple[OutcomeSkip, ...]
    already: int                       # rows previously recorded (idempotent re-encounter)
    topups: tuple[str, ...] = ()       # analysis-only bar top-up notes, one per symbol

    def summary(self) -> str:
        base = (f"scorecard: +{len(self.written)} outcomes" if self.written
                else "scorecard: none matured")
        if self.topups:
            base += f" · topped up {len(self.topups)} analysis-only symbol(s)"
        return base


def vindicated(recommendation: str | None, excess: Decimal,
               *, shadow: bool) -> bool | None:
    """The scoring rule, in one place (module docstring): BUY wants excess > 0,
    REJECT wants excess < 0; HOLD/other and shadow memos grade as None —
    tracked, never counted in a vindication rate."""
    if shadow:
        return None
    if recommendation == "BUY":
        return excess > 0
    if recommendation == "REJECT":
        return excess < 0
    return None


def dissent_right(recommendation: str | None, excess: Decimal,
                  *, shadow: bool) -> bool | None:
    """The dissent graded at read time (module docstring): the EXACT
    complement of vindicated() for directional memos — every committee memo
    records the strongest case against its own call, so a call that was not
    vindicated means the dissent was right. Dead heats grade the dissent
    right (conservative against the desk). HOLD/other and shadow memos: None
    — no gradable direction, no gradable dissent."""
    v = vindicated(recommendation, excess, shadow=shadow)
    return None if v is None else not v


def dartboard_baseline(recommendation: str | None,
                       excesses: Sequence[Decimal]) -> Decimal | None:
    """The base rate a direction-blind dart scores against the SAME tracked
    outcomes (module docstring): the fraction of `excesses` whose sign
    matches the slice's vindication direction — excess > 0 for BUY,
    excess < 0 for REJECT, dead heats for neither. `excesses` is EVERY
    tracked outcome at the horizon (HOLD and shadow rows included: the dart
    throws at everything the fund tracked, blind to what the desk said).
    None when nothing is tracked yet, or for non-directional slices.
    Subtracting this from a slice's vindication rate is what stops an
    always-REJECT desk in a falling market from looking smart: the dart
    matches it, and the edge reads zero."""
    if not excesses:
        return None
    if recommendation == "BUY":
        hits = sum(1 for e in excesses if e > 0)
    elif recommendation == "REJECT":
        hits = sum(1 for e in excesses if e < 0)
    else:
        return None
    return Decimal(hits) / Decimal(len(excesses))


def anchor_index(bar_dates: Sequence[date], memo_date: date) -> int | None:
    """Index of the last session at or before memo_date; None when the memo
    predates the whole series (fail-closed, never a forward anchor)."""
    i = bisect_right(bar_dates, memo_date) - 1
    return i if i >= 0 else None


def plan_outcomes(
        memos: Sequence[MemoRef],
        series: Mapping[str, Sequence[tuple[date, Decimal]]],
        spy: Mapping[date, Decimal],
        existing: AbstractSet[tuple[str, int]],
        *, horizons: Sequence[int] = HORIZONS,
) -> tuple[list[PlannedOutcome], list[OutcomeSkip], int]:
    """Pure planning core: (rows to insert, skips with reasons, already-count).

    `series` maps each RESOLVED symbol to its ascending priceable session
    sequence [(bar_date, close), ...]; a symbol absent from the mapping did
    not resolve to exactly one active instrument. `spy` maps bar_date ->
    close for the benchmark. `existing` holds (memo_id, horizon) pairs
    already recorded — idempotency lives here, mirrored by the UNIQUE
    constraint at the database. Deterministic: same inputs, same plan,
    memo order preserved."""
    rows: list[PlannedOutcome] = []
    skips: list[OutcomeSkip] = []
    already = 0
    for memo in memos:
        if memo.symbol is None or memo.symbol not in series:
            skips.append(OutcomeSkip(
                memo_id=memo.memo_id, symbol=memo.symbol, horizon_sessions=None,
                reason="no instrument (symbol does not resolve to exactly one "
                       "active instrument, or one row of any activity)"))
            continue
        bars = series[memo.symbol]
        dates = [d for d, _ in bars]
        a = anchor_index(dates, memo.memo_date)
        if a is None:
            skips.append(OutcomeSkip(
                memo_id=memo.memo_id, symbol=memo.symbol, horizon_sessions=None,
                reason=f"missing bars (no vendor bar on or before "
                       f"{memo.memo_date})"))
            continue
        anchor_date, anchor_close = bars[a]
        if anchor_close <= 0:
            skips.append(OutcomeSkip(
                memo_id=memo.memo_id, symbol=memo.symbol, horizon_sessions=None,
                reason=f"missing bars (non-positive anchor close on "
                       f"{anchor_date})"))
            continue
        for h in horizons:
            if (memo.memo_id, h) in existing:
                already += 1
                continue
            f = a + h
            if f >= len(bars):
                skips.append(OutcomeSkip(
                    memo_id=memo.memo_id, symbol=memo.symbol, horizon_sessions=h,
                    reason=f"immature (needs {h} sessions past {anchor_date}, "
                           f"has {len(bars) - 1 - a})"))
                continue
            fwd_date, fwd_close = bars[f]
            if fwd_close <= 0:
                skips.append(OutcomeSkip(
                    memo_id=memo.memo_id, symbol=memo.symbol, horizon_sessions=h,
                    reason=f"missing bars (non-positive forward close on "
                           f"{fwd_date})"))
                continue
            spy_anchor = spy.get(anchor_date)
            spy_fwd = spy.get(fwd_date)
            if spy_anchor is None or spy_anchor <= 0 or spy_fwd is None:
                missing = anchor_date if (spy_anchor is None or spy_anchor <= 0) \
                    else fwd_date
                skips.append(OutcomeSkip(
                    memo_id=memo.memo_id, symbol=memo.symbol, horizon_sessions=h,
                    reason=f"missing bars ({BENCHMARK_SYMBOL} has no usable "
                           f"close on {missing})"))
                continue
            fwd_return = (fwd_close / anchor_close - 1).quantize(_RETURN_QUANT)
            spy_return = (spy_fwd / spy_anchor - 1).quantize(_RETURN_QUANT)
            rows.append(PlannedOutcome(
                memo_id=memo.memo_id, symbol=memo.symbol,
                recommendation=memo.recommendation, shadow=memo.shadow,
                horizon_sessions=h, anchor_date=anchor_date,
                anchor_close=anchor_close, fwd_date=fwd_date,
                fwd_close=fwd_close, fwd_return=fwd_return,
                spy_return=spy_return, excess=fwd_return - spy_return))
    return rows, skips, already


@dataclass(frozen=True)
class ResolvedInstrument:
    """What grading (and the analysis-only top-up) needs to know about the
    one instrument a memo symbol resolved to."""
    instrument_id: str
    exchange: str
    market: str
    is_active: bool


def _resolve_instrument(session: Session, symbol: str) -> ResolvedInstrument | None:
    """Exactly one ACTIVE instrument wins — the rule the bridge applies. With
    no active row, exactly one row of ANY activity resolves too (module
    docstring): grading needs bars, not tradability, and analyze-box memos
    live on analysis-only is_active=FALSE rows. Ambiguity fails closed."""
    rows = session.execute(text(
        "SELECT id, exchange, market, is_active FROM market.instruments "
        "WHERE symbol = :s"), {"s": symbol}).all()
    active = [r for r in rows if r.is_active]
    if len(active) == 1:
        pick = active[0]
    elif not active and len(rows) == 1:
        pick = rows[0]
    else:
        return None
    return ResolvedInstrument(instrument_id=str(pick.id), exchange=pick.exchange,
                              market=pick.market, is_active=bool(pick.is_active))


def _load_series(session: Session, instrument_id: str,
                 through: date) -> list[tuple[date, Decimal]]:
    """The instrument's priceable session sequence: ascending vendor bars with
    a non-NULL close, hard-capped at the clock's date (no look-ahead), and
    SPLIT-ADJUSTED on read (module docstring) using only splits recorded with
    action_date <= `through`. Degenerate OHLC below exists solely to satisfy
    the Bar invariant; only close is read back."""
    rows = session.execute(text(
        "SELECT bar_date, close FROM market.price_bars_daily "
        "WHERE instrument_id = :iid AND source = :src "
        "  AND close IS NOT NULL AND bar_date <= :d ORDER BY bar_date"),
        {"iid": instrument_id, "src": VENDOR_SOURCE, "d": through}).all()
    splits = [Split(symbol=instrument_id, action_date=r.action_date,
                    ratio=Decimal(r.ratio))
              for r in session.execute(text(
                  "SELECT action_date, ratio FROM market.corporate_actions "
                  "WHERE instrument_id = :iid AND action_type = 'split' "
                  "  AND action_date <= :d ORDER BY action_date"),
                  {"iid": instrument_id, "d": through}).all()]
    if not splits:
        return [(r.bar_date, r.close) for r in rows]
    bars = [Bar(symbol=instrument_id, bar_date=r.bar_date, open=r.close,
                high=r.close, low=r.close, close=r.close, volume=0)
            for r in rows]
    return [(b.bar_date, b.close) for b in adjust_for_splits(bars, splits)]


def vendor_adapter_for(symbol: str, exchange: str) -> MarketDataAdapter:
    """Adapter factory for the analysis-only top-up — what the t9 call site
    passes as `adapter_for`. The daily cycle's own adapter maps only
    seed/universe symbols and refuses bare pass-through, so an analysis-only
    symbol needs its own single-entry map (the ops/analyze construction,
    same vendor_symbol rule; unknown exchanges still fail loudly there).
    Keyless local development gets the deterministic fixture adapter,
    exactly like the daily ingest."""
    settings = get_settings()
    if settings.eodhd_api_key:
        return EodhdAdapter(settings.eodhd_api_key,
                            symbol_map={symbol: vendor_symbol(symbol, exchange)})
    return FixtureAdapter(_REPO / "tests" / "fixtures")


def _top_up_inactive_bars(
        session: Session, clock: Clock,
        adapter_for: Callable[[str, str], MarketDataAdapter],
        awaiting: AbstractSet[str],
        instruments: Mapping[str, ResolvedInstrument]) -> tuple[str, ...]:
    """Bounded forward-bar maintenance for analysis-only instruments (module
    docstring): nightly ingest skips inactive rows, so without this their
    memo outcomes never mature. Only inactive instruments among `awaiting`
    (symbols with committee memos still missing an outcome row), only the
    missing window — sessions after the latest stored vendor bar through the
    last completed session at the injected clock; a bar outside that window
    is never stored (no partials, no look-ahead). Splits are recorded
    alongside so the split-adjusted read stays honest. No stored vendor bars
    = no top-up (grading fails closed on its own; this is maintenance, never
    a backfill). Vendor failures are fail-soft per symbol and noted — an SQL
    failure still aborts the transaction like any node-level SQL failure
    (the scanner's caveat, ops/daily.py)."""
    notes: list[str] = []
    now = clock.now()
    for symbol in sorted(awaiting):
        inst = instruments.get(symbol)
        if inst is None or inst.is_active:
            continue                   # active instruments are t0 ingest's job
        latest = session.execute(text(
            "SELECT max(bar_date) FROM market.price_bars_daily "
            "WHERE instrument_id = :iid AND source = :src"),
            {"iid": inst.instrument_id, "src": VENDOR_SOURCE}).scalar()
        if latest is None:
            continue
        days = incremental_sessions(inst.market, latest, now)
        if not days:
            continue                   # already current through the last close
        start, end = days[0], days[-1]
        try:
            adapter = adapter_for(symbol, inst.exchange)
            source = type(adapter).__name__
            for sp in adapter.fetch_splits(symbol, start, end):
                record_split(session, inst.instrument_id, sp, source)
            n_bars = 0
            for b in adapter.fetch_bars(symbol, start, end):
                if not start <= b.bar_date <= end:
                    continue           # never a partial or forward bar
                upsert_bar(session, inst.instrument_id, b, source)
                n_bars += 1
        except Exception as e:  # noqa: BLE001 — fail-soft per symbol, never silent
            notes.append(f"{symbol}: top-up failed ({str(e)[:120]})")
            continue
        notes.append(f"{symbol}: +{n_bars} bars through {end}")
    return tuple(notes)


def compute_memo_outcomes(
        session: Session, clock: Clock,
        adapter_for: Callable[[str, str], MarketDataAdapter] | None = None,
) -> ScorecardReport:
    """Grade every committee memo whose outcomes have matured (module
    docstring). Inserts matured (memo, horizon) rows exactly once, counts
    skips with reasons, and appends ONE research.scorecard.updated audit
    event when — and only when — new rows landed. `adapter_for` (optional,
    ops-injected) enables the bounded analysis-only bar top-up before
    grading; None keeps this a pure read of stored tables."""
    now = clock.now()
    today = now.astimezone(UTC).date()

    memo_rows = session.execute(text(
        "SELECT m.id, m.instrument_symbol, m.recommendation, m.created_at, "
        "       COALESCE(ar.shadow, false) AS shadow "
        "FROM research.memos m "
        "LEFT JOIN research.agent_runs ar ON ar.id = m.agent_run_id "
        "WHERE m.memo_type = 'committee' AND m.created_at <= :now "
        "ORDER BY m.created_at, m.id"), {"now": now}).all()
    memos = [MemoRef(memo_id=str(r.id), symbol=r.instrument_symbol,
                     recommendation=r.recommendation, shadow=bool(r.shadow),
                     memo_date=r.created_at.astimezone(UTC).date())
             for r in memo_rows]

    existing = {(str(r.memo_id), int(r.horizon_sessions))
                for r in session.execute(text(
                    "SELECT memo_id, horizon_sessions "
                    "FROM research.memo_outcomes")).all()}

    instruments: dict[str, ResolvedInstrument] = {}
    for symbol in sorted({m.symbol for m in memos if m.symbol is not None}):
        inst = _resolve_instrument(session, symbol)
        if inst is not None:
            instruments[symbol] = inst

    topups: tuple[str, ...] = ()
    if adapter_for is not None:
        awaiting = {m.symbol for m in memos
                    if m.symbol is not None
                    and any((m.memo_id, h) not in existing for h in HORIZONS)}
        topups = _top_up_inactive_bars(session, clock, adapter_for,
                                       awaiting, instruments)

    series = {symbol: _load_series(session, inst.instrument_id, today)
              for symbol, inst in instruments.items()}

    spy: dict[date, Decimal] = {}
    spy_inst = _resolve_instrument(session, BENCHMARK_SYMBOL)
    if spy_inst is not None:           # no SPY => every horizon skips fail-closed
        spy = dict(_load_series(session, spy_inst.instrument_id, today))

    rows, skips, already = plan_outcomes(memos, series, spy, existing)

    for r in rows:
        session.execute(text(
            "INSERT INTO research.memo_outcomes "
            "(memo_id, horizon_sessions, anchor_date, anchor_close, fwd_close, "
            " fwd_return, spy_return, excess, computed_at) "
            "VALUES (:m, :h, :ad, :ac, :fc, :fr, :sr, :ex, :t) "
            "ON CONFLICT (memo_id, horizon_sessions) DO NOTHING"),
            {"m": r.memo_id, "h": r.horizon_sessions, "ad": r.anchor_date,
             "ac": r.anchor_close, "fc": r.fwd_close, "fr": r.fwd_return,
             "sr": r.spy_return, "ex": r.excess, "t": now})

    if rows:
        payload: dict[str, object] = {
            "written": len(rows),
            "memo_ids": sorted({r.memo_id for r in rows}),
            "by_horizon": {str(h): sum(1 for r in rows
                                       if r.horizon_sessions == h)
                           for h in HORIZONS}}
        if topups:
            payload["topups"] = list(topups)
        PostgresAuditLog(session, clock).append(
            event_type="research.scorecard.updated", entity_type="scorecard",
            entity_id=today.isoformat(), actor_type="dcp", actor_id="scorecard",
            payload=payload)
    return ScorecardReport(written=tuple(rows), skipped=tuple(skips),
                           already=already, topups=topups)
