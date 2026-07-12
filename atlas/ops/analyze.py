"""ANALYZE-ANY-TICKER: on-demand desk analysis of one symbol from the console.

The Principal types a ticker (e.g. an investing.com pick) with an optional
source tag; this module prepares the data and runs the FULL agent desk on
exactly that symbol — debate, grounding verifier, budget breaker, audit chain,
memo persistence. Nothing is bypassed: the memo lands through committee_memo
like any nightly memo, tagged with the source (migration 0017), and a cage
hold is reported honestly as the system working.

Mirrors atlas/ops/scheduler.py exactly: a non-blocking lock (one analysis at
a time — "busy" is an answer, not an error), a background worker thread, and
a module-level status dict the API polls. Wall clock here is legitimate: this
is the ops layer deciding WHEN and observing progress; the desk itself still
receives an injectable Clock. Ops importing the agents package is the
established precedent (atlas/ops/daily.py imports the desk for T7).

Data preparation:
- UNKNOWN symbol: inserted as an ANALYSIS-ONLY instrument — is_active=FALSE,
  so it stays invisible to the scanner, the nightly desk, quality gates and
  every tradable-universe surface (same invisibility contract as the
  validation-only instruments in dcp/market_data/validation_universe.py).
  US-only in v1: the vendor code is {SYM}.US via vendor_symbol(symbol, 'US')
  — a non-US ticker needs exchange/market/currency/calendar decisions that
  are out of scope here (India exposure is via ETFs/ADRs per ADR-0002).
  ~ANALYZE_SESSIONS sessions of daily bars are fetched and stored RAW with
  splits recorded (the storage convention: adjustment happens on read), plus
  one fundamentals snapshot.
- KNOWN symbol: topped up with the existing staleness conventions — bars via
  incremental_sessions from the latest stored bar, fundamentals only when the
  latest snapshot is older than FUNDAMENTALS_STALE_DAYS (both from
  dcp/market_data/daily.py).
- No quality gates are written: gate coverage is a tradable-universe contract
  (same rationale as backfill --symbols mode).
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.desk import DeskReport, run_desk
from atlas.agents.runtime.runner import budget_surface
from atlas.core.clock import SystemClock
from atlas.core.config import get_settings
from atlas.core.db import session_scope
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter, vendor_symbol
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.calendars import last_completed_session, recent_sessions
from atlas.dcp.market_data.daily import FUNDAMENTALS_STALE_DAYS, incremental_sessions
from atlas.dcp.market_data.ingest import record_split, upsert_bar

_REPO = Path(__file__).resolve().parents[2]

# ~400 completed sessions (~19 months): comfortably beyond build_evidence's
# >=51-bar requirement and the 60-bar evidence window, with slack for the
# indicators — without turning a console click into a deep-history backfill.
ANALYZE_SESSIONS = 400

_analysis_lock = threading.Lock()
_status: dict[str, object] = {"phase": "idle", "symbol": None, "source": None,
                              "started_at": None, "finished_at": None,
                              "detail": None, "result": None}


def _build_adapter(symbol: str, exchange: str) -> MarketDataAdapter:
    """Vendor adapter with a single-entry symbol map for THIS analysis —
    unknown symbols are not in any seeds/manifest map, so the map is built
    from the instrument's exchange via the same vendor_symbol rule every
    other surface uses (unknown exchanges still fail loudly there). The
    fixture adapter serves keyless local development, exactly like the
    daily ingest."""
    settings = get_settings()
    if settings.eodhd_api_key:
        return EodhdAdapter(settings.eodhd_api_key,
                            symbol_map={symbol: vendor_symbol(symbol, exchange)})
    return FixtureAdapter(_REPO / "tests" / "fixtures")


def _resolve_instrument(session: Session, symbol: str) -> tuple[object, str, str, bool]:
    """(instrument_id, exchange, market, known). Unknown symbols are inserted
    as analysis-only rows: is_active=FALSE (invisible to scanner/desk-nightly/
    gates), US-only v1, instrument_type left NULL — the type is honestly
    unknown until a human classifies it. Prefers the active row when a symbol
    exists on several exchanges (same symbol-keyed convention as the loaders,
    which validation seeding keeps collision-free)."""
    row = session.execute(text(
        "SELECT id, exchange, market FROM market.instruments WHERE symbol = :s "
        "ORDER BY is_active DESC, exchange LIMIT 1"), {"s": symbol}).mappings().first()
    if row is not None:
        return row["id"], row["exchange"], row["market"], True
    iid = session.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, name, currency, "
        " is_active) "
        "VALUES (:s, 'US', 'US', :n, 'USD', FALSE) RETURNING id"),
        {"s": symbol, "n": f"{symbol} (analysis-only)"}).scalar_one()
    return iid, "US", "US", False


def _prepare_data(session: Session, adapter: MarketDataAdapter, symbol: str,
                  instrument_id: object, market: str, now: datetime) -> str:
    """Bars (raw, with splits recorded) + one fundamentals snapshot; returns
    the honest one-line report. Zero stored bars after the fetch is a hard
    failure — the desk cannot argue about a symbol with no history. A
    fundamentals vendor failure is fail-soft: the desk keeps its bar/indicator
    evidence and extract_fundamentals_evidence honestly returns None."""
    source = type(adapter).__name__
    latest = session.execute(text(
        "SELECT max(bar_date) FROM market.price_bars_daily "
        "WHERE instrument_id = :i"), {"i": instrument_id}).scalar()
    end = last_completed_session(market, now)
    if latest is None:
        fetch_from = recent_sessions(market, end, lookback=ANALYZE_SESSIONS - 1)[0]
    else:
        days = incremental_sessions(market, latest, now)
        fetch_from = days[0] if days else None

    n_bars = n_splits = 0
    if fetch_from is not None:
        for sp in adapter.fetch_splits(symbol, fetch_from, end):
            record_split(session, instrument_id, sp, source)
            n_splits += 1
        for b in adapter.fetch_bars(symbol, fetch_from, end):
            if not fetch_from <= b.bar_date <= end:
                continue  # never store a bar outside the completed window
            upsert_bar(session, instrument_id, b, source)
            n_bars += 1
    stored = session.execute(text(
        "SELECT count(*) FROM market.price_bars_daily WHERE instrument_id = :i"),
        {"i": instrument_id}).scalar()
    if not stored:
        raise RuntimeError(f"no daily bars available for {symbol} — the vendor "
                           "returned nothing for the requested window")

    today = now.astimezone(UTC).date()
    latest_f = session.execute(text(
        "SELECT max(as_of) FROM market.fundamentals WHERE instrument_id = :i"),
        {"i": instrument_id}).scalar()
    if latest_f is not None and (today - latest_f).days <= FUNDAMENTALS_STALE_DAYS:
        f_line = "fundamentals fresh"
    else:
        try:
            payload = adapter.fetch_fundamentals(symbol)
        except Exception as e:  # noqa: BLE001 — fail-soft, but never silent
            f_line = f"fundamentals unavailable ({str(e)[:80]})"
        else:
            session.execute(text(
                "INSERT INTO market.fundamentals (instrument_id, as_of, payload, "
                " source) VALUES (:i, :d, CAST(:p AS jsonb), :src) "
                "ON CONFLICT (instrument_id, as_of) DO NOTHING"),
                {"i": instrument_id, "d": today, "p": json.dumps(payload),
                 "src": source})
            f_line = "fundamentals fetched"
    return (f"bars +{n_bars} (stored {int(stored)}), splits +{n_splits}, {f_line}")


def _interpret(report: DeskReport) -> tuple[dict[str, object], str]:
    """One symbol in, exactly one outcome out — memo, cage hold, or skip, all
    verbatim and all 'done': a held cage or an honest skip is the system
    working, never an infrastructure failure."""
    if report.memos:
        m = report.memos[0]
        return ({"outcome": "memo", "recommendation": m.recommendation,
                 "conviction": m.conviction},
                f"memo landed: {m.recommendation} (conviction {m.conviction})")
    if report.cage_holds:
        _, why = report.cage_holds[0]
        return ({"outcome": "cage_held", "reason": why},
                f"CAGE HELD — run failed closed, no memo landed: {why}")
    if report.skipped:
        _, why = report.skipped[0]
        return ({"outcome": "skipped", "reason": why}, f"skipped: {why}")
    return ({"outcome": "none"}, "desk produced no outcome for the symbol")


def _run_analysis(symbol: str, source: str | None) -> None:
    now = datetime.now(UTC)
    with session_scope() as s:
        iid, exchange, market, known = _resolve_instrument(s, symbol)
        adapter = _build_adapter(symbol, exchange)
        prep = _prepare_data(s, adapter, symbol, iid, market, now)
    # data committed before the desk runs: build_evidence reads stored bars
    _status.update(phase="analyzing",
                   detail=f"data ready ({'known' if known else 'new analysis-only'} "
                          f"instrument; {prep}) — desk debating")
    with session_scope() as s:
        try:
            # Per-surface budget sub-cap (desk-review 2026-07 item 6): every
            # run in this analysis counts against ATLAS_BUDGET_ANALYZE (default
            # $3.00) inside the global $10 breaker, so an analyze spree can
            # never starve the nightly desk — precedence and watermark
            # semantics documented in atlas/agents/runtime/runner.py.
            with budget_surface("analyze"):
                report = run_desk(s, SystemClock(), [symbol], source=source)
        except Exception:
            # failed runs' cost + audit trail must persist — the budget
            # breaker counts them (live_run.py precedent); a rollback here
            # would hide real spend
            s.commit()
            raise
    result, detail = _interpret(report)
    _status.update(phase="done", finished_at=datetime.now(UTC).isoformat(),
                   result=result, detail=detail)


def start_analysis(symbol: str, source: str | None) -> bool:
    """Console trigger. Returns False when an analysis is already running —
    one at a time; the caller reports 'busy' honestly, nothing runs twice.
    `symbol` is expected upcased/validated by the API layer; `source` is
    stored verbatim (it never enters a prompt — see cio.py)."""
    if not _analysis_lock.acquire(blocking=False):
        return False
    _status.update(phase="fetching", symbol=symbol, source=source,
                   started_at=datetime.now(UTC).isoformat(), finished_at=None,
                   detail="fetching data (bars + fundamentals)", result=None)

    def _target() -> None:
        try:
            _run_analysis(symbol, source)
        except Exception as e:  # noqa: BLE001 — the ops layer survives anything
            _status.update(phase="failed",
                           finished_at=datetime.now(UTC).isoformat(),
                           detail=str(e)[:300])
        finally:
            _analysis_lock.release()

    threading.Thread(target=_target, name="atlas-analyze", daemon=True).start()
    return True


def analysis_status() -> dict[str, object]:
    """Snapshot copy (same discipline as scheduler.status): mutating the
    returned dict must never reach the live status."""
    out = dict(_status)
    if isinstance(out.get("result"), dict):
        out["result"] = dict(out["result"])  # type: ignore[arg-type]
    out["running"] = _analysis_lock.locked()
    return out
