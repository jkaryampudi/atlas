"""Memo->proposal bridge (ADR-0006 deterministic stop derivation).

The research desk emits committee memos; this module is the ONLY path from a
memo to a trade proposal, and it derives every number from vendor bars alone —
the agent chose WHAT to propose, the DCP alone chooses THE NUMBERS (CLAUDE.md
invariant 2: no agent-produced value may reach sizing, pricing or execution).
Pure deterministic compute plane: no agent import (two-plane wall), injectable
Clock only, one audit event per run.

ADR-0006 Decision, implemented to the letter:
  entry  = latest EODHD close for the symbol (<= the clock's date, fail-closed
           staleness via the shared _latest_close mark);
  stop   = max(entry - 2 x ATR(14), entry x 0.90) — volatility-scaled with a
           hard -10% floor;
  target = entry + 2 x (entry - stop) — a 2R objective, recorded for review
           honesty, nothing executes on it in v1;
  qty    = the risk engine's size_position via build_proposal, unchanged —
           sizing remains an output of risk, never conviction.
ATR(14) is Wilder's average true range over vendor high/low/close; fewer than
15 complete OHLC sessions means NO proposal (fail closed, recorded as skipped).

Candidates: committee memos with recommendation='BUY' whose agent run is not
a shadow run (ADR-0005 pattern 4: shadow output is non-actionable; a NULL
agent_run_id counts as non-shadow — hand-authored memos carry no shadow flag),
created within the last 48h of the injected clock. Older BUY memos are stale
theses: the desk runs nightly, so a memo two cycles old was either bridged
already or blocked twice, and market context has moved past its evidence —
they fall out of CANDIDACY (not recorded as skips, or every historical memo
would re-appear in every report forever).

Scope guards (ADR-0006: one live proposal per symbol), each recorded as a
skip with its reason:
- a proposal already references the memo (idempotency: the same memo never
  bridges twice, in ANY state — a risk-FAIL 'rejected' outcome is final for
  that memo, not retried nightly);
- the memo has no evidence refs (Principle 1: no trade without evidence —
  signal_ids are derived from the refs and must be non-empty);
- the symbol does not resolve to exactly one active instrument;
- the symbol has an open position, a live proposal in
  ('risk_review','pending_approval','approved') — action-agnostic: an
  in-flight exit blocks new entries too — or a live order in
  ('pending_submit','submitted','partially_filled');
- the instrument has a KNOWN future earnings report within
  EARNINGS_GUARD_SESSIONS XNYS sessions of the decision date (skip
  'earnings_print_imminent', report date in the reason) — the position would
  enter at the next open and face the print almost immediately (risk-wiring
  bundle 2026-07-18). Missing calendar data is NO block: the absence of a
  known print is not evidence of one, and market.earnings_calendar only
  claims the prints the vendor reported;
- the symbol was stopped out within the last REENTRY_COOLING_SESSIONS XNYS
  sessions AND the memo predates that exit (skip 'reentry_cooling') — Doc 03
  prohibited activities: no re-entry into a stopped-out name within 10
  trading days without a new committee memo. A memo created AFTER the
  stop-out IS the new committee memo and passes: that is the signed policy's
  own exception, not a loophole;
- price derivation failed closed (no/stale vendor close, or an incomplete
  ATR window).

No-averaging-down (Doc 03 prohibited activities): the open-position guard
above IS the policy's call site. bridge_memos is the ONLY live caller of
build_proposal (pinned by tests/unit/test_policy_conformance.py), and a memo
for a symbol with an open position is ALWAYS a recorded skip — so the agent
lane structurally cannot add to an existing position, at any price, past any
budget. The add-on merge branch in proposals._record_fill is therefore
unreachable through the agent lane; the core-allocation lane (ADR-0014) tops
up passive index ETFs by signed target weight through its own builder, which
is rebalancing under an ADR, not discretionary averaging down.

Documented resolutions (ADR-0006 ambiguities, resolved conservatively):
- The ATR window is EXACTLY the last 15 sessions on or before the clock's
  date. Wilder smoothing has infinite memory, so the window length is part of
  the deterministic policy; 15 is the minimum the ADR permits (14 TRs with a
  previous close plus the seed bar). Any NULL high/low/close INSIDE that
  window fails closed — the bridge never reaches past an incomplete bar for
  an older complete one (that would derive volatility from a hand-picked
  history).
- signal_ids: an evidence ref that IS a quant signal ref
  ('dcp:signal:<family>:<uuid>:<date>' for family xsmom OR pead, emitted only
  by signals/{xsmom,pead}/generate.extract_*signal_evidence) resolves to the
  REAL quant.signals UUID — verified to exist; a signal-shaped ref whose row is
  missing fails the memo closed (a forged lineage must never silently become
  a synthetic one). Every other ref keeps ADR-0006's interim
  uuid5(NAMESPACE_URL, 'atlas:evidence:<ref>') convention — memos without
  signals bridge exactly as before. Deduplicated preserving order; the full
  ref->uuid mapping lands in the trading.bridge.completed audit payload so
  lineage stays reconstructible.

- sleeve budget (ADR-0014, option B — the active satellite is momentum 10% +
  PEAD 10% of NAV): a memo attributed to a signed strategy FAMILY (via its
  resolved signal_ids -> quant.signals -> strategy_id, the same lineage join
  bands.py uses) is sized so the family's AGGREGATE new exposure stays inside
  its envelope. Per name: floor((NAV*fraction - already_committed) / n_names /
  (entry x FX)) whole shares, equal-weight across the sleeve's BUY names — a
  hard cap PASSED INTO build_proposal that can only SHRINK the risk engine's §4
  size, never grow it (the engine still validates the capped quantity; risk may
  shrink it further). already_committed sums the family's open positions and its
  live unfilled proposals. Without this cap ~10 momentum names would each size
  to the L1 8% single-name limit and aggregate to ~77% of NAV — far past the
  signed 10% sleeve. Memos with no signed-sleeve signal are sized by risk alone,
  exactly as before.
- A build_proposal risk FAIL (state 'rejected') is an HONEST outcome reported
  in BridgeReport.built with its verdict — the gate working is a deliverable,
  never an error (CLAUDE.md working style).
- Stop/target are quantized to the 6dp price quantum shared with the
  execution plane (trading.trade_proposals numeric(18,6)); the float ATR
  enters Decimal via Decimal(str(...)) exactly like paper.py's CostModel bps
  — never binary-float artifacts in a ledger number.
- _lifecycle_lock is taken FIRST, so candidate selection and the scope-guard
  reads serialise against approvals/settlements; build_proposal re-takes the
  same pg advisory xact lock, which is re-entrant within one transaction
  (verified: Postgres advisory-lock requests by a session already holding the
  lock always succeed; xact-scoped locks release together at commit).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.execution.paper import PRICE_SOURCE, fx_to_aud
from atlas.dcp.indicators.core import wilder_atr
from atlas.dcp.market_data.calendars import next_trading_day, trading_days_between
from atlas.dcp.trading.proposals import (
    ProposalResult,
    _audit,
    _build_book,
    _latest_close,
    _lifecycle_lock,
    _load_instrument,
    build_proposal,
)

MEMO_MAX_AGE = timedelta(hours=48)     # older BUY memos are stale theses

# Risk-wiring bundle (Principal, 2026-07-18). Both constants are POLICY:
# changing either is a reviewed diff, exactly like a stop multiplier.
#
# EARNINGS_GUARD_SESSIONS: a candidate whose instrument has a KNOWN future
# report within this many XNYS sessions of the decision date is skipped
# ('earnings_print_imminent') — entry fills at the next session's open, so a
# print inside this window lands on a position hours-to-one-day old, with the
# memo's evidence entirely pre-print. XNYS is the anchor calendar for the
# decision cadence (the daily cycle keys off US sessions) and applies to every
# candidate regardless of listing market. Only STRICTLY FUTURE report dates
# block: a print dated the decision day itself has already happened (or
# happens tonight, before the fill) — the entry never holds through it.
EARNINGS_GUARD_SESSIONS = 2

# REENTRY_COOLING_SESSIONS: Doc 03 prohibited activities — "no re-entry into a
# stopped-out name within 10 trading days without a new committee memo". A
# stop-exit disposal (order_type='stop' sell fill) starts the clock; fewer
# than this many XNYS sessions elapsed (strictly after the exit's fill date,
# through the decision date) blocks a memo created BEFORE the exit
# ('reentry_cooling'). A memo created after the stop-out is the policy's own
# "new committee memo" exception and passes.
REENTRY_COOLING_SESSIONS = 10

ATR_PERIOD = 14                        # ADR-0006: ATR(14)
ATR_WINDOW = ATR_PERIOD + 1            # 15 sessions: 14 full TRs + seed bar
STOP_ATR_MULT = Decimal(2)             # ADR-0006: entry - 2 x ATR(14)
STOP_FLOOR = Decimal("0.90")           # ADR-0006: hard -10% floor
TARGET_R_MULT = Decimal(2)             # ADR-0006: 2R objective
_PRICE_QUANT = Decimal("0.000001")     # trade_proposals numeric(18,6) quantum


@dataclass(frozen=True)
class BridgedProposal:
    symbol: str
    memo_id: str
    proposal_id: str
    verdict: str                       # 'PASS' | 'FAIL' — a FAIL is honest
    qty: int


@dataclass(frozen=True)
class BridgeSkip:
    symbol: str                        # the memo's raw instrument_symbol
    memo_id: str
    reason: str


@dataclass(frozen=True)
class BridgeReport:
    built: tuple[BridgedProposal, ...] = ()
    skipped: tuple[BridgeSkip, ...] = ()

    def summary(self) -> str:
        outcomes = ", ".join(f"{b.symbol}:{b.verdict}" for b in self.built) or "none"
        return f"bridged {len(self.built)} ({outcomes}) · skipped {len(self.skipped)}"


def evidence_signal_id(ref: str) -> uuid.UUID:
    """ADR-0006 interim signal identity: a deterministic UUIDv5 of the memo's
    DCP evidence ref — the same ref always maps to the same id, so the
    audit-recorded mapping reconstructs lineage. Since migration 0020 this is
    the FALLBACK for refs that are not quant signal refs (see
    _resolve_signal_ids); memos without signals keep bridging on it."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"atlas:evidence:{ref}")


# 'dcp:signal:<family>:<quant.signals uuid>:<signal_date>' — emitted only by
# signals/{xsmom,pead}/generate.extract_*signal_evidence, so the embedded uuid
# IS the real signal identity the sleeve attribution joins on. The family group
# is NON-capturing so the uuid stays group(1) for _resolve_signal_ids.
_SIGNAL_REF = re.compile(
    r"^dcp:signal:(?:xsmom|pead):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}):\d{4}-\d{2}-\d{2}$")

# ADR-0014 (option B, signed 2026-07-16) as amended by ADR-0015 (2026-07-18):
# the active satellite is momentum 10% of NAV; the PEAD sleeve is SUSPENDED at
# 0.00 (its implementable form failed the null model — random top-5 draws did
# as well, p=0.132 — so its BUY memos size to an honest zero/skip while its
# signals, memos and scorecard record keep accruing as a forward experiment).
# Core 70%, cash 20%. SLEEVE_BUDGET_FRACTION keys each signed strategy FAMILY
# (quant.strategies.family) to its fraction of NAV — the same
# documented-constant pattern core_allocation.CORE_TARGETS uses for the passive
# core. A family absent here has no sleeve cap (sized by risk alone); a family
# at 0.00 is a capital-suspended sleeve whose membership still records for
# attribution but never allocates. Editing these numbers is an ADR change,
# exactly like editing CORE_TARGETS.
SLEEVE_BUDGET_FRACTION: dict[str, Decimal] = {
    "xsmom-pit-tr": Decimal("0.10"),   # momentum sleeve (ADR-0014)
    "pead-sue-tr": Decimal("0.00"),    # PEAD sleeve SUSPENDED (ADR-0015)
}


def _resolve_signal_ids(session: Session, refs: list[str]) -> dict[str, str]:
    """{ref: uuid-str} preserving first-occurrence order: quant signal refs
    resolve to their REAL quant.signals id (verified against the table — a
    signal-shaped ref with no row raises _SkipMemo, fail closed), everything
    else to the ADR-0006 interim uuid5."""
    out: dict[str, str] = {}
    for ref in dict.fromkeys(refs):
        m = _SIGNAL_REF.match(ref)
        if m is None:
            out[ref] = str(evidence_signal_id(ref))
            continue
        sid = uuid.UUID(m.group(1))
        if session.execute(text(
                "SELECT 1 FROM quant.signals WHERE id = :sid"),
                {"sid": sid}).first() is None:
            raise _SkipMemo(f"memo cites quant signal {sid} but no such row "
                            "exists — refusing to fabricate signal lineage")
        out[ref] = str(sid)
    return out


# ------------------------------------------------------ sleeve budget (ADR-0014)

def _signal_ref_uuids(refs: list[str]) -> list[uuid.UUID]:
    """The quant.signals UUIDs embedded in a memo's signal-shaped evidence refs
    — the candidates for sleeve attribution (non-signal refs are ignored)."""
    out: list[uuid.UUID] = []
    for ref in refs:
        m = _SIGNAL_REF.match(ref)
        if m is not None:
            out.append(uuid.UUID(m.group(1)))
    return out


def _sleeve_families(session: Session, signal_uuids: list[uuid.UUID]) -> list[str]:
    """EVERY signed-sleeve family a memo's signal UUIDs belong to (sorted), or
    []. The attribution join is the one bands.py and _sleeve_committed_aud use —
    a signal id maps to its strategy's family — restricted to the paper/live
    sleeves that carry a budget.

    ALL families, not LIMIT 1 (audit 2026-07-17): a dual-winner name (both a
    momentum and a PEAD top-5) is a member of BOTH sleeves under the intersect
    attribution rule, so it must occupy a budget slot in both and be sized
    under the TIGHTER slice — attributing it to one family let the other
    deploy its full envelope while still carrying the shared name's exposure
    (a breach of the signed 10%). The shared name eating a slot in each sleeve
    under-deploys the aggregate slightly: conservative, never over."""
    if not signal_uuids:
        return []
    rows = session.execute(text(
        "SELECT DISTINCT st.family FROM quant.signals s "
        "JOIN quant.strategies st ON st.id = s.strategy_id "
        "WHERE s.id = ANY(:ids) AND st.state IN ('paper','live') "
        "  AND st.family = ANY(:fams) ORDER BY st.family"),
        {"ids": signal_uuids, "fams": list(SLEEVE_BUDGET_FRACTION)}).all()
    return [str(r.family) for r in rows]


def _sleeve_committed_aud(session: Session, family: str, on: date) -> Decimal:
    """Capital already committed to `family`'s sleeve at run start: the market
    value of its OPEN positions PLUS the reserved value of its LIVE (unfilled)
    proposals. Same signal-lineage attribution the band check uses (bands.py):
    a lot / proposal is in the sleeve iff its proposal's signal_ids intersect
    the family's paper/live quant.signals ids. Both terms only ENLARGE the
    committed base, so the remaining envelope can only shrink sizing — a
    conservative superset of the ADR-0014 'existing sleeve positions' that also
    closes the cross-cycle over-commit hole (yesterday's still-pending sleeve
    proposals count against today's budget)."""
    sleeve_ids = ("ARRAY(SELECT s.id FROM quant.signals s "
                  "JOIN quant.strategies st ON st.id = s.strategy_id "
                  "WHERE st.family = :fam AND st.state IN ('paper','live'))")
    total = Decimal("0")
    fx_cache: dict[str, Decimal] = {}
    for r in session.execute(text(
            "SELECT tp.instrument_id AS iid, i.currency, tl.qty "
            "FROM trading.tax_lots tl "
            "JOIN trading.executions e ON e.id = tl.execution_id "
            "JOIN trading.orders o ON o.id = e.order_id "
            "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
            "JOIN market.instruments i ON i.id = tp.instrument_id "
            "WHERE tl.disposed_at IS NULL AND tp.signal_ids && " + sleeve_ids),
            {"fam": family}).all():
        if r.currency not in fx_cache:
            fx_cache[r.currency] = fx_to_aud(session, r.currency, on)
        total += Decimal(int(r.qty)) * _latest_close(session, r.iid, on) \
            * fx_cache[r.currency]
    reserved = session.execute(text(
        "SELECT COALESCE(sum(position_value_aud), 0) FROM trading.trade_proposals "
        "WHERE state IN ('risk_review','pending_approval','approved') "
        "  AND signal_ids && " + sleeve_ids), {"fam": family}).scalar_one()
    return total + Decimal(reserved)


def _sleeve_name_budgets(session: Session, clock: Clock,
                         candidates: Sequence[Any]) -> dict[str, Decimal]:
    """Per-name AUD slice for each sleeve family with BUY candidates this run,
    computed ONCE at run start (a snapshot — a proposal created later in the run
    does not shrink a sibling's slice; the sleeve's names split one fixed
    envelope equally). For family F with n candidate BUY names:

        per_name_aud = (NAV * SLEEVE_BUDGET_FRACTION[F] - committed_F) / n

    NAV is the book NAV the risk engine sizes against (worst-case pro-forma,
    _build_book). n counts every candidate attributed to F; a name later skipped
    by a scope guard simply leaves its slice undeployed (aggregate stays UNDER
    budget — conservative). A non-positive slice (envelope already full) yields a
    sub-1-share cap downstream and the name is skipped."""
    counts: dict[str, int] = {}
    for memo in candidates:
        refs = [r for r in memo.evidence_refs if isinstance(r, str) and r]
        for fam in _sleeve_families(session, _signal_ref_uuids(refs)):
            counts[fam] = counts.get(fam, 0) + 1   # dual name: a slot in EACH
    if not counts:
        return {}
    nav = _build_book(session, clock).state.nav_aud
    on = clock.now().date()
    return {fam: (nav * SLEEVE_BUDGET_FRACTION[fam]
                  - _sleeve_committed_aud(session, fam, on)) / n
            for fam, n in counts.items()}


def derive_prices(entry: Decimal, atr14: float) -> tuple[Decimal, Decimal]:
    """ADR-0006 Decision applied to one entry price: (stop, target).

    stop = max(entry - 2 x ATR14, entry x 0.90), target = entry + 2R where
    R = entry - stop. The float ATR crosses into Decimal via str() (the
    paper.py CostModel-bps convention); both outputs carry the 6dp price
    quantum. target is derived from the QUANTIZED stop, so the recorded 2R
    is exact against the recorded stop."""
    stop = max(entry - STOP_ATR_MULT * Decimal(str(atr14)),
               entry * STOP_FLOOR).quantize(_PRICE_QUANT)
    target = (entry + TARGET_R_MULT * (entry - stop)).quantize(_PRICE_QUANT)
    return stop, target


class _SkipMemo(Exception):
    """Internal: a recorded, fail-closed per-memo skip (never propagates)."""


def _atr14(session: Session, instrument_id: uuid.UUID, clock: Clock) -> float:
    """Wilder ATR(14) over EXACTLY the last ATR_WINDOW vendor sessions on or
    before the clock's date. Fail closed (raise _SkipMemo): fewer than 15
    sessions, or any NULL high/low/close inside the window."""
    rows = session.execute(text(
        "SELECT high, low, close FROM ("
        "  SELECT bar_date, high, low, close FROM market.price_bars_daily "
        "  WHERE instrument_id = :iid AND source = :src AND bar_date <= :d "
        "  ORDER BY bar_date DESC LIMIT :n) w ORDER BY bar_date"),
        {"iid": instrument_id, "src": PRICE_SOURCE, "d": clock.now().date(),
         "n": ATR_WINDOW}).all()
    if len(rows) < ATR_WINDOW:
        raise _SkipMemo(f"only {len(rows)} vendor sessions on record — "
                        f"ATR(14) needs {ATR_WINDOW} (ADR-0006 fail-closed)")
    if any(r.high is None or r.low is None or r.close is None for r in rows):
        raise _SkipMemo(f"incomplete OHLC inside the {ATR_WINDOW}-session ATR "
                        "window (NULL high/low/close) — fail closed (ADR-0006)")
    atr = wilder_atr([float(r.high) for r in rows], [float(r.low) for r in rows],
                     [float(r.close) for r in rows], period=ATR_PERIOD)[-1]
    assert atr is not None  # structural: len(rows) == ATR_WINDOW > ATR_PERIOD
    return atr


def _guard_scope(session: Session, instrument_id: uuid.UUID, symbol: str) -> None:
    """ADR-0006 one-live-proposal-per-symbol guards; raise _SkipMemo on any."""
    if session.execute(text(
            "SELECT 1 FROM trading.positions WHERE instrument_id = :iid "
            "AND closed_at IS NULL AND qty > 0 LIMIT 1"),
            {"iid": instrument_id}).first() is not None:
        raise _SkipMemo(f"{symbol} has an open position")
    if session.execute(text(
            "SELECT 1 FROM trading.trade_proposals WHERE instrument_id = :iid "
            "AND state IN ('risk_review','pending_approval','approved') LIMIT 1"),
            {"iid": instrument_id}).first() is not None:
        raise _SkipMemo(f"{symbol} has a live proposal awaiting the lifecycle")
    if session.execute(text(
            "SELECT 1 FROM trading.orders o "
            "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
            "WHERE tp.instrument_id = :iid "
            "AND o.state IN ('pending_submit','submitted','partially_filled') "
            "LIMIT 1"), {"iid": instrument_id}).first() is not None:
        raise _SkipMemo(f"{symbol} has a live order in flight")


def _guard_earnings_print(session: Session, instrument_id: uuid.UUID,
                          symbol: str, on: date) -> None:
    """Risk-wiring bundle: skip a candidate facing a known print within
    EARNINGS_GUARD_SESSIONS XNYS sessions (constant's comment has the full
    semantics). Missing calendar data = no block, by design: the query only
    ever matches prints the vendor actually reported."""
    horizon = on
    for _ in range(EARNINGS_GUARD_SESSIONS):
        horizon = next_trading_day("US", horizon)
    row = session.execute(text(
        "SELECT report_date FROM market.earnings_calendar "
        "WHERE instrument_id = :iid AND report_date > :on "
        "  AND report_date <= :h ORDER BY report_date LIMIT 1"),
        {"iid": instrument_id, "on": on, "h": horizon}).first()
    if row is not None:
        raise _SkipMemo(
            f"earnings_print_imminent: {symbol} has a known earnings report on "
            f"{row.report_date.isoformat()} — within {EARNINGS_GUARD_SESSIONS} "
            f"XNYS sessions of {on.isoformat()}; the position would enter at "
            "the next open and face the print almost immediately")


def _guard_reentry_cooling(session: Session, instrument_id: uuid.UUID,
                           symbol: str, *, memo_created_at: datetime,
                           on: date) -> None:
    """Doc 03 re-entry cooling (constant's comment has the full semantics):
    the LATEST stop-exit fill for the instrument starts the clock; a memo
    created before that exit is blocked until REENTRY_COOLING_SESSIONS XNYS
    sessions have elapsed. A memo created after the stop-out is the signed
    policy's own 'new committee memo' exception and passes."""
    exited_at = session.execute(text(
        "SELECT max(e.executed_at) FROM trading.executions e "
        "JOIN trading.orders o ON o.id = e.order_id "
        "JOIN trading.trade_proposals tp ON tp.id = o.proposal_id "
        "WHERE tp.instrument_id = :iid AND o.side = 'sell' "
        "  AND o.order_type = 'stop'"), {"iid": instrument_id}).scalar()
    if exited_at is None:
        return                                   # never stopped out
    exit_date = exited_at.astimezone(UTC).date()
    elapsed = len(trading_days_between("US", exit_date + timedelta(days=1), on))
    if elapsed >= REENTRY_COOLING_SESSIONS:
        return                                   # cooling period served
    if memo_created_at > exited_at:
        return  # post-stop-out memo IS the policy's "new committee memo"
    raise _SkipMemo(
        f"reentry_cooling: {symbol} was stopped out on {exit_date.isoformat()} "
        f"and only {elapsed} of {REENTRY_COOLING_SESSIONS} XNYS sessions have "
        "elapsed; the memo predates that exit — re-entry inside the cooling "
        "window requires a NEW committee memo (Doc 03 prohibited activities)")


def bridge_memos(session: Session, clock: Clock) -> BridgeReport:
    """Bridge every candidate BUY memo into a risk-checked trade proposal
    (module docstring; ADR-0006). Per-memo skips are recorded outcomes; an
    unexpected exception propagates to the caller (the daily cycle pages).
    Emits ONE trading.bridge.completed audit event per run carrying the
    built proposal ids, the skips with reasons, and the full evidence
    ref->uuid mapping (lineage stays reconstructible)."""
    _lifecycle_lock(session)   # re-entrant with build_proposal's (docstring)
    now = clock.now()
    candidates = session.execute(text(
        "SELECT m.id, m.instrument_symbol, m.evidence_refs, m.created_at "
        "FROM research.memos m "
        "LEFT JOIN research.agent_runs ar ON ar.id = m.agent_run_id "
        "WHERE m.memo_type = 'committee' AND m.recommendation = 'BUY' "
        "  AND COALESCE(ar.shadow, false) = false "
        "  AND m.instrument_symbol IS NOT NULL "
        "  AND m.created_at > :cutoff AND m.created_at <= :now "
        "ORDER BY m.created_at, m.id"),
        {"cutoff": now - MEMO_MAX_AGE, "now": now}).all()

    # ADR-0014 sleeve budgets: the per-name AUD slice for each satellite family
    # with BUY candidates tonight, snapshotted BEFORE any proposal is built so
    # the sleeve's names split one fixed envelope equally (module: sleeve budget).
    sleeve_budgets = _sleeve_name_budgets(session, clock, candidates)

    built: list[BridgedProposal] = []
    skipped: list[BridgeSkip] = []
    ref_map: dict[str, dict[str, str]] = {}   # memo_id -> {ref: uuid}
    for memo in candidates:
        memo_id, symbol = str(memo.id), str(memo.instrument_symbol)
        try:
            if session.execute(text(
                    "SELECT 1 FROM trading.trade_proposals "
                    "WHERE committee_memo_id = :m LIMIT 1"),
                    {"m": memo.id}).first() is not None:
                raise _SkipMemo("memo already bridged (a proposal references "
                                "it — idempotent, any state)")
            refs = [r for r in memo.evidence_refs if isinstance(r, str) and r]
            if not refs:
                raise _SkipMemo("memo has no evidence refs — no trade without "
                                "evidence (Principle 1)")
            try:
                inst = _load_instrument(session, symbol)
            except ValueError as e:   # not exactly one active instrument
                raise _SkipMemo(str(e)) from e
            _guard_scope(session, inst.id, symbol)
            _guard_earnings_print(session, inst.id, symbol, now.date())
            _guard_reentry_cooling(session, inst.id, symbol,
                                   memo_created_at=memo.created_at,
                                   on=now.date())
            try:                      # fail-closed mark: missing/stale close
                entry = _latest_close(session, inst.id, now.date())
            except RuntimeError as e:
                raise _SkipMemo(str(e)) from e
            stop, target = derive_prices(entry, _atr14(session, inst.id, clock))
            signal_ids = _resolve_signal_ids(session, refs)
            # ADR-0014 sleeve cap: a memo attributed to a signed satellite
            # family gets a whole-share cap = floor(per-name AUD slice /
            # entry x FX), equal-weight across the sleeve's BUY names. It only
            # ever SHRINKS the §4 risk size (build_proposal.sleeve_max_qty);
            # non-sleeve memos are sized by risk alone (cap None). A full
            # envelope (< 1 share fits) is an honest recorded skip.
            sleeve_cap: int | None = None
            fams = _sleeve_families(session, _signal_ref_uuids(refs))
            if fams:
                # ADR-0015: only FUNDED sleeves govern sizing. A zero-budget
                # (suspended) sleeve's membership records for attribution but
                # must not veto a funded one — a dual momentum+PEAD winner
                # deploys under momentum's slice. A name whose ONLY sleeve is
                # suspended sizes to zero (honest recorded skip).
                funded = [f for f in fams if SLEEVE_BUDGET_FRACTION[f] > 0]
                if not funded:
                    raise _SkipMemo(
                        f"{symbol}: sleeve(s) {'/'.join(fams)} suspended at "
                        "zero budget (ADR-0015) — memo recorded, no capital")
                price_aud = entry * fx_to_aud(session, inst.currency, now.date())
                # dual-winner name: sized under the TIGHTER of its FUNDED
                # sleeves' slices so no funded family exceeds its envelope
                per_name = min(sleeve_budgets[f] for f in funded)
                sleeve_cap = int(
                    (per_name / price_aud).to_integral_value(ROUND_FLOOR))
                if sleeve_cap < 1:
                    raise _SkipMemo(
                        f"{symbol}: {'/'.join(funded)} sleeve envelope full — "
                        f"per-name budget A${per_name:.2f} buys no whole share "
                        f"at A${price_aud:.2f} (ADR-0014)")
            res: ProposalResult = build_proposal(
                session, clock, memo_id=memo_id, symbol=symbol,
                signal_refs=list(dict.fromkeys(signal_ids.values())),
                entry_price=entry,
                stop_price=stop, target_price=target,
                sleeve_max_qty=sleeve_cap)
        except _SkipMemo as e:
            skipped.append(BridgeSkip(symbol=symbol, memo_id=memo_id,
                                      reason=str(e)))
            continue
        ref_map[memo_id] = signal_ids
        built.append(BridgedProposal(symbol=symbol, memo_id=memo_id,
                                     proposal_id=res.proposal_id,
                                     verdict=res.verdict, qty=res.qty))

    _audit(session, clock).append(
        event_type="trading.bridge.completed", entity_type="bridge",
        entity_id=now.date().isoformat(), actor_type="dcp",
        actor_id="memo_bridge",
        payload={
            "built": [{"memo_id": b.memo_id, "symbol": b.symbol,
                       "proposal_id": b.proposal_id, "verdict": b.verdict,
                       "qty": b.qty} for b in built],
            "skipped": [{"memo_id": k.memo_id, "symbol": k.symbol,
                         "reason": k.reason} for k in skipped],
            "evidence_signal_ids": ref_map,
        })
    return BridgeReport(built=tuple(built), skipped=tuple(skipped))
