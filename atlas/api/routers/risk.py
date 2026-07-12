"""Risk surface (Doc 06: /risk/limit-sets/current, /risk/drawdown, and the
dual-confirmation breaker-clearance flow).

Reads report the engine's governing configuration and state — honestly,
including 'not effective yet' and 'no NAV series yet' provenance. The ONE
write path is the Doc 04 §5 resumption action: POST /breaker-clearances
(confirmation A) and POST /breaker-clearances/{id}/confirm (confirmation B,
≥1h later — DUAL_CONFIRM_TOO_SOON otherwise, Doc 06 §3.3). State changes
happen in atlas.dcp.risk.clearance and audit themselves; this layer only
maps outcomes to the §3.3 uniform error envelope, exactly like the trading
router.

POST /preflight (the what-if dry run) and GET /correlations (the L8 heat
matrix) are ALSO reads, despite the POST verb: a what-if is a question, not a
material action — no proposal row, no risk-check row, no audit event (audit
records decisions; recording questions would bury them). Documented
resolutions for both live on the handlers.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from atlas.core.clock import Clock, SystemClock
from atlas.core.db import session_scope
from atlas.dcp.execution.paper import fx_to_aud
from atlas.dcp.risk import clearance
from atlas.dcp.risk import correlations as corrfeed
from atlas.dcp.risk import engine
from atlas.dcp.trading import proposals as lifecycle

router = APIRouter()


def _clock() -> Clock:
    """Seam for tests (CLAUDE.md invariant 6: injectable time). Production
    uses wall time; the ≥1h dual-confirmation gap makes that self-gating."""
    return SystemClock()


def _envelope(status: int, code: str, message: str,
              details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message, "details": details}})


def _value_error_response(e: ValueError) -> JSONResponse:
    msg = str(e)
    if "unknown" in msg:
        return _envelope(404, "NOT_FOUND", msg)
    if "DUAL_CONFIRM_TOO_SOON" in msg:
        return _envelope(409, "DUAL_CONFIRM_TOO_SOON", msg)
    return _envelope(409, "INVALID_STATE", msg)

# Doc 04 §3 — rule descriptions for the register (values come from the DB)
RULES: list[tuple[str, str, str]] = [
    ("L1", "L1_max_stock_weight", "Max single-stock weight at cost"),
    ("L2", "L2_max_etf_weight", "Max single-ETF weight"),
    ("L3", "L3_max_sector_exposure", "Max GICS sector exposure (pro-forma)"),
    ("L4", "L4_max_india_sleeve", "Max India sleeve incl. ADR/ETF look-through"),
    ("L5", "L5_min_cash_reserve", "Min cash reserve"),
    ("L6", "L6_max_risk_per_trade", "Max portfolio risk per trade (entry−stop × size)"),
    ("L7", "L7_max_aggregate_open_risk", "Max aggregate open risk to stops"),
    ("L8", "L8_corr_threshold", "Pairwise correlation threshold (with combined-weight cap)"),
    ("L9", "L9_max_new_positions_per_day", "Max new positions per day"),
    ("L10", "L10_max_pct_adv", "Max position vs 20-day ADV"),
    ("L11", "L11_max_non_aud_exposure", "Max unhedged non-AUD exposure"),
]


@router.get("/limit-set/current")
def limit_set_current() -> dict[str, object]:
    with session_scope() as s:
        row = s.execute(text(
            "SELECT version, mode, limits, effective_from, created_by "
            "FROM risk.limit_sets ORDER BY version DESC LIMIT 1")).mappings().first()
    if row is None:
        return {"seeded": False, "active": False, "register": []}
    limits = dict(row["limits"])
    eff: date = row["effective_from"]
    register = [{"rule": r, "description": d, "value": limits.get(k)}
                for r, k, d in RULES]
    register.append({"rule": "L8b", "description": "L8 combined-weight cap",
                     "value": limits.get("L8_corr_combined_weight")})
    return {"seeded": True,
            "version": row["version"], "mode": row["mode"],
            "effective_from": eff.isoformat(),
            "active": eff <= date.today(),
            "created_by": row["created_by"],
            "register": register}


@router.get("/breakers")
def breakers() -> dict[str, object]:
    """Drawdown circuit-breaker ladder (Doc 04 §5) plus the CURRENT latched
    level: the fold over persisted NAV snapshots + confirmed clearances (the
    book-independent view — no live mark, so this read can never fail on a
    stale close)."""
    with session_scope() as s:
        n = s.execute(text(
            "SELECT count(*) FROM trading.portfolio_snapshots")).scalar_one()
        level = clearance.latched_breaker_level(s)
    provenance = (
        "no NAV series yet — breaker state computes from portfolio snapshots; "
        "DD2/DD3 latch until dual-confirmed human clearance" if n == 0 else
        f"latched fold over {n} NAV snapshots; DD2/DD3 latch until "
        "dual-confirmed human clearance (Doc 04 §5)")
    return {
        "current_level": level.value.upper(),
        "provenance": provenance,
        "ladder": [
            {"level": "DD1", "trigger_pct": -5,
             "action": "New-position risk halved (L6 → 0.5%); CIO review memo required"},
            {"level": "DD2", "trigger_pct": -10,
             "action": "No new positions; full-book re-underwrite; human review to resume"},
            {"level": "DD3", "trigger_pct": -15,
             "action": "FULL HALT — exit-only; per-holding human keep/exit decision; "
                       "post-mortem before re-arming"},
        ],
    }


class ClearanceRequestBody(BaseModel):
    reason: str


@router.post("/breaker-clearances")
def request_breaker_clearance(body: ClearanceRequestBody) -> Any:
    """Confirmation A of the Doc 04 §5 resumption action. 409 INVALID_STATE
    when there is nothing to clear (breaker NONE/DD1) or a request is already
    pending."""
    try:
        with session_scope() as s:
            cid = clearance.request_clearance(s, _clock(), reason=body.reason)
    except ValueError as e:
        return _value_error_response(e)
    return {"status": "pending_confirmation", "clearance_id": cid}


@router.post("/breaker-clearances/{clearance_id}/confirm")
def confirm_breaker_clearance(clearance_id: str) -> Any:
    """Confirmation B. 409 DUAL_CONFIRM_TOO_SOON before requested_at + 1h
    (Doc 06 §3.3); 404 for an unknown id; 409 INVALID_STATE if already
    confirmed. Returns the recomputed latched level."""
    try:
        with session_scope() as s:
            level = clearance.confirm_clearance(s, _clock(),
                                                clearance_id=clearance_id)
    except ValueError as e:
        return _value_error_response(e)
    return {"status": "cleared", "clearance_id": clearance_id,
            "latched_level": level.value}


@router.get("/breaker-clearances")
def breaker_clearances(limit: int = 20) -> list[dict[str, object]]:
    """Recent clearance requests, pending first — the console renders the
    pending one with its not-before instant."""
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT id, from_level, reason, requested_by, requested_at, "
            "       confirmed_at "
            "FROM risk.breaker_clearances "
            "ORDER BY (confirmed_at IS NULL) DESC, requested_at DESC "
            "LIMIT :n"), {"n": limit}).mappings().all()
    return [{"id": str(r["id"]), "from_level": r["from_level"],
             "reason": r["reason"], "requested_by": r["requested_by"],
             "requested_at": r["requested_at"].isoformat(),
             "confirmable_after": (r["requested_at"]
                                   + clearance.DUAL_CONFIRM_GAP).isoformat(),
             "confirmed_at": (r["confirmed_at"].isoformat()
                              if r["confirmed_at"] else None),
             "pending": r["confirmed_at"] is None}
            for r in rows]


# --------------------------------------------------- what-if pre-flight (dry run)

class PreflightBody(BaseModel):
    symbol: str
    entry_price: Decimal
    stop_price: Decimal


_ADVISORY = ("advisory only — a real proposal re-checks fresh at approval "
             "time; a pre-flight PASS is never a pre-commitment")


@router.post("/preflight")
def preflight(body: PreflightBody) -> Any:
    """WHAT-IF RISK PRE-FLIGHT: a strictly READ-ONLY dry run of the proposal
    path — active limits, live worst-case pro-forma book, §4 sizing, and (when
    sizing accepts) the full itemised L1-L11 engine.validate. ZERO writes: no
    proposal row, no risk-check row, no audit event — a what-if is a question,
    not a material action, and the audit chain records decisions, not
    questions (Doc 01 §8 reconstructs decisions; a browsing principal must not
    be able to bury them under hypotheticals).

    Documented resolutions:
    - The trading lifecycle's pg advisory xact lock (_lifecycle_lock) is NOT
      taken. It exists to serialise check-then-WRITE races (two approvals each
      re-checking against a book that excludes the other's order); a pure read
      has no write to race. The cost of taking it would be real: pre-flights
      fired from the console would queue behind — and delay — settlements and
      approvals. Worst case without it is a book read mid-commit of another
      transaction (ordinary MVCC snapshot semantics), which can only make an
      ADVISORY answer momentarily stale — exactly what the advisory label
      already warns, and the real proposal path re-checks fresh under the
      lock.
    - Same evaluation order as build_proposal: limits -> instrument -> book ->
      size -> validate. No limit set effective on the query date answers the
      honest 409 (the engine refuses to run — Doc 04 §10); an unknown or
      inactive symbol answers 404; malformed prices answer 400 before any DB
      read (structurally invalid, not a risk verdict).
    - A §4 sizing rejection reports as a FAIL with the single itemised rule
      'SIZING' — mirroring how build_proposal persists it (sizing IS risk
      policy); L1-L11 are not evaluated for a trade that has no size.
    - Fail-closed book errors (unmarkable holding, missing FX) propagate as
      500 exactly as they would on the real build path: a book that cannot be
      marked cannot honestly answer what-ifs, and inventing a degraded answer
      would weaken the risk read (CLAUDE.md invariant 3).
    - session.rollback() before returning makes the zero-write claim
      structural, not just observed: even if a reused helper ever grew a
      write, nothing from this path could commit."""
    if body.entry_price <= 0 or body.stop_price <= 0:
        return _envelope(400, "INVALID_PRICES",
                         "entry and stop prices must be positive")
    if body.stop_price >= body.entry_price:
        return _envelope(400, "INVALID_PRICES",
                         "stop must be below entry (long-only mandate)")
    clock = _clock()
    on = clock.now().date()
    with session_scope() as s:
        try:
            limits = engine.load_active_limit_set(s, on)  # raises before effective_from
        except RuntimeError as e:
            return _envelope(409, "NO_ACTIVE_LIMIT_SET", str(e))
        try:
            inst = lifecycle._load_instrument(s, body.symbol)
        except ValueError as e:
            return _envelope(404, "NOT_FOUND", str(e))
        book = lifecycle._build_book(s, clock)
        size = engine.size_position(
            nav_aud=book.state.nav_aud, entry_price=body.entry_price,
            stop_price=body.stop_price, fx_to_aud=fx_to_aud(s, inst.currency, on),
            instrument_type=inst.instrument_type,
            adv_20d=lifecycle._adv_20d(s, inst.id, on),
            limits=limits, breaker=book.breaker)
        if size.accepted:
            proposal = lifecycle._fresh_proposal_inputs(
                s, clock, inst, qty=size.qty, entry_price=body.entry_price,
                stop_price=body.stop_price, book=book)
            check = engine.validate(proposal, book.state, limits, book.breaker)
            verdict = "PASS" if check.passed else "FAIL"
            results = [{"rule": r.rule, "pass": r.passed,
                        "value": float(r.value) if r.value is not None else None,
                        "limit": float(r.limit) if r.limit is not None else None,
                        "detail": r.detail} for r in check.results]
        else:  # §4 'reject if …' — same single-rule shape build_proposal persists
            verdict = "FAIL"
            results = [{"rule": "SIZING", "pass": False, "value": None,
                        "limit": None,
                        "detail": f"{size.detail} "
                                  f"(binding: {size.binding_constraint})"}]
        s.rollback()   # structural zero-write guarantee (docstring)
    return {"symbol": inst.symbol, "qty": size.qty,
            "binding_constraint": size.binding_constraint,
            "sizing_accepted": size.accepted, "sizing_detail": size.detail,
            "verdict": verdict, "breaker": book.breaker.value,
            "results": results, "limit_set_version": limits.version,
            "nav_aud": float(book.state.nav_aud), "advisory": _ADVISORY}


# ------------------------------------------------------- L8 correlation matrix

CORR_MAX_SYMBOLS = 12          # 66 pair computations; the console stays legible
CORR_WINDOW_SESSIONS = 90      # the L8 window (Doc 04 §3, correlations.py)


def _display_corr(closes_a: dict[date, Decimal],
                  closes_b: dict[date, Decimal]) -> float | None:
    """One DISPLAY cell from date->close maps, aligned exactly like the L8
    feed's _pair_correlation. The one deliberate divergence, documented: where
    the ENGINE fails closed to Decimal("1") (thin overlap, non-positive
    closes, zero variance — correlations.py), the display answers None. Fail
    closed is a GATING posture; painting a fabricated 1.0 in a heat matrix
    would be fake data presented as measurement. The console dims None as
    'n/a' and the note states the engine's worst-case treatment."""
    common = sorted(set(closes_a) & set(closes_b))
    if len(common) < corrfeed.MIN_OVERLAP_RETURNS + 1:   # k returns need k+1 closes
        return None
    a = [float(closes_a[d]) for d in common]
    b = [float(closes_b[d]) for d in common]
    if min(a) <= 0.0 or min(b) <= 0.0:
        return None
    returns_a = [a[i] / a[i - 1] - 1.0 for i in range(1, len(a))]
    returns_b = [b[i] / b[i - 1] - 1.0 for i in range(1, len(b))]
    try:
        return float(corrfeed.pairwise_correlation(returns_a, returns_b))
    except ValueError:   # zero variance
        return None


@router.get("/correlations")
def correlations(symbols: str | None = None) -> dict[str, object]:
    """L8 HEAT MATRIX: pairwise 90-session return correlations over the
    symbols the desk actually cares about — open positions plus the latest
    scanner shortlist by default, or an explicit ?symbols=A,B,C list. Reuses
    the L8 feed's machinery (same window, same vendor-source filter, same
    no-look-ahead cap at the clock date) via correlations.pairwise_correlation
    and _load_closes.

    Documented resolutions:
    - Insufficient data -> null, never fake (module convention): see
      _display_corr. The diagonal is 1.0 only when the symbol has a usable
      window; a symbol with no usable closes shows null even vs itself —
      'trivially 1' would disguise 'no data at all'.
    - Default symbol set: open positions first (alphabetical), then the most
      recent scanner.completed audit event's shortlist in shortlist order,
      deduplicated, capped at CORR_MAX_SYMBOLS. An explicit list is capped the
      same way (first 12 kept, capped=true reported) — a hard 400 would make
      the console's default set fragile for no safety gain on a read.
    - Symbols not matching an ACTIVE instrument are reported in 'unknown' and
      excluded from the matrix rather than rendered as an all-null row: the
      caller asked about something the system does not trade; say so.
    - No writes, no lock: pure read, same reasoning as /preflight."""
    clock = _clock()
    end = clock.now().date()
    with session_scope() as s:
        if symbols is not None:
            requested = list(dict.fromkeys(
                t.strip() for t in symbols.split(",") if t.strip()))
            source = "explicit"
        else:
            held = [r.symbol for r in s.execute(text(
                "SELECT i.symbol FROM trading.positions p "
                "JOIN market.instruments i ON i.id = p.instrument_id "
                "WHERE p.closed_at IS NULL AND p.qty > 0 ORDER BY i.symbol"))]
            payload = s.execute(text(
                "SELECT payload FROM audit.decision_events "
                "WHERE event_type = 'scanner.completed' "
                "ORDER BY created_at DESC, seq DESC LIMIT 1")).scalar()
            shortlist = [e["symbol"] for e in (payload or {}).get("shortlist", [])
                         if e.get("symbol")]
            requested = list(dict.fromkeys(held + shortlist))
            source = "book+scanner"
        capped = len(requested) > CORR_MAX_SYMBOLS
        requested = requested[:CORR_MAX_SYMBOLS]

        active = {r.symbol for r in s.execute(text(
            "SELECT symbol FROM market.instruments "
            "WHERE is_active AND symbol = ANY(:syms)"),
            {"syms": requested})} if requested else set()
        known = [sym for sym in requested if sym in active]
        unknown = [sym for sym in requested if sym not in active]

        closes = {sym: corrfeed._load_closes(s, sym, end=end,
                                             window=CORR_WINDOW_SESSIONS)
                  for sym in known}
    usable = {sym for sym in known
              if len(closes[sym]) >= corrfeed.MIN_OVERLAP_RETURNS + 1}
    n = len(known)
    matrix: list[list[float | None]] = [[None] * n for _ in range(n)]
    for i in range(n):
        if known[i] in usable:
            matrix[i][i] = 1.0
        for j in range(i + 1, n):
            matrix[i][j] = matrix[j][i] = _display_corr(closes[known[i]],
                                                        closes[known[j]])
    return {"symbols": known, "unknown": unknown, "matrix": matrix,
            "window_sessions": CORR_WINDOW_SESSIONS, "end": end.isoformat(),
            "min_overlap_returns": corrfeed.MIN_OVERLAP_RETURNS,
            "source": source, "capped": capped,
            "note": ("engine treatment differs by design: thin/degenerate "
                     "pairs gate as worst-case 1 (fail closed); the display "
                     "shows null, never a fabricated number")}
