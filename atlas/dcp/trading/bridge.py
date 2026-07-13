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
- price derivation failed closed (no/stale vendor close, or an incomplete
  ATR window).

Documented resolutions (ADR-0006 ambiguities, resolved conservatively):
- The ATR window is EXACTLY the last 15 sessions on or before the clock's
  date. Wilder smoothing has infinite memory, so the window length is part of
  the deterministic policy; 15 is the minimum the ADR permits (14 TRs with a
  previous close plus the seed bar). Any NULL high/low/close INSIDE that
  window fails closed — the bridge never reaches past an incomplete bar for
  an older complete one (that would derive volatility from a hand-picked
  history).
- signal_ids: an evidence ref that IS a quant signal ref
  ('dcp:signal:xsmom:<uuid>:<date>', emitted only by
  signals/xsmom/generate.extract_signal_evidence) resolves to the REAL
  quant.signals UUID — verified to exist; a signal-shaped ref whose row is
  missing fails the memo closed (a forged lineage must never silently become
  a synthetic one). Every other ref keeps ADR-0006's interim
  uuid5(NAMESPACE_URL, 'atlas:evidence:<ref>') convention — memos without
  signals bridge exactly as before. Deduplicated preserving order; the full
  ref->uuid mapping lands in the trading.bridge.completed audit payload so
  lineage stays reconstructible.
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
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.clock import Clock
from atlas.dcp.execution.paper import PRICE_SOURCE
from atlas.dcp.indicators.core import wilder_atr
from atlas.dcp.trading.proposals import (
    ProposalResult,
    _audit,
    _latest_close,
    _lifecycle_lock,
    _load_instrument,
    build_proposal,
)

MEMO_MAX_AGE = timedelta(hours=48)     # older BUY memos are stale theses
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


# 'dcp:signal:xsmom:<quant.signals uuid>:<signal_date>' — emitted only by
# signals/xsmom/generate.extract_signal_evidence, so the embedded uuid IS the
# real signal identity the sleeve attribution joins on
_SIGNAL_REF = re.compile(
    r"^dcp:signal:xsmom:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}):\d{4}-\d{2}-\d{2}$")


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
        "SELECT m.id, m.instrument_symbol, m.evidence_refs "
        "FROM research.memos m "
        "LEFT JOIN research.agent_runs ar ON ar.id = m.agent_run_id "
        "WHERE m.memo_type = 'committee' AND m.recommendation = 'BUY' "
        "  AND COALESCE(ar.shadow, false) = false "
        "  AND m.instrument_symbol IS NOT NULL "
        "  AND m.created_at > :cutoff AND m.created_at <= :now "
        "ORDER BY m.created_at, m.id"),
        {"cutoff": now - MEMO_MAX_AGE, "now": now}).all()

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
            try:                      # fail-closed mark: missing/stale close
                entry = _latest_close(session, inst.id, now.date())
            except RuntimeError as e:
                raise _SkipMemo(str(e)) from e
            stop, target = derive_prices(entry, _atr14(session, inst.id, clock))
            signal_ids = _resolve_signal_ids(session, refs)
            res: ProposalResult = build_proposal(
                session, clock, memo_id=memo_id, symbol=symbol,
                signal_refs=list(dict.fromkeys(signal_ids.values())),
                entry_price=entry,
                stop_price=stop, target_price=target)
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
