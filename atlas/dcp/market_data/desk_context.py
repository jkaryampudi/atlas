"""Cheap deterministic desk-evidence blocks (desk-review 2026-07 item 10).

Two extractors, both pure DCP reads rendered as (ref, body) tuples for
build_evidence, both returning None when the record is absent — the desk
keeps its current evidence set; a fabricated line is never an option.

1. MARKET REGIME (item 10a): the P3 regime classifier
   (atlas/dcp/signals/regime/v1.py — closed vocabulary bull / bear /
   high_vol / neutral, strictly causal) run over SPY's stored vendor bars
   up to the evidence date. Resolutions:
   - Fixed input window: the last REGIME_LOOKBACK_BARS SPY closes. The
     classifier's high_vol threshold uses an expanding median over the fed
     series, so the label depends on series length — a pinned window keeps
     the label deterministic for a given evidence date instead of drifting
     with backfill depth.
   - Warmup honesty: with fewer than TREND_WINDOW + 1 bars the classifier
     emits its warmup placeholder ("neutral"), which is a coverage artifact,
     not a market fact — the extractor returns None instead of rendering it.
   - Split-adjust on read (desk-review item 2), splits known on or before
     the evidence date only; closes-only path, degenerate OHLC objects are
     never read back.

2. SCANNER CONTEXT (item 10b): when the memo run comes from the nightly
   shortlist, the desk should know WHY the scanner routed the name — the
   score components from the latest scanner.completed audit payload,
   rendered numeric-only and explicitly labelled "attention, not prediction"
   (ADR-0007: the scanner makes no alpha claim; nothing here is validated).
   Resolutions:
   - The event must cover the SAME session as the memo's evidence date (the
     payload's per-market `sessions` value equals `on`): a scan of another
     session is another cross-section and is not rendered — analyze-box runs
     and stale scans get None, never a mismatched rank.
   - Audit payloads are our own DCP output, but every field is still read
     fail-closed (typed, pattern-checked) — a malformed or legacy payload
     yields None, not a crash and never partial text.
   - Numbers render as plain decimal literals (the fundamentals _number
     convention) so a memo quoting them grounds verbatim under the
     token-boundary grounding verifier.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.backtest.engine import OBar
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.models import Bar, Split
from atlas.dcp.signals.regime.v1 import TREND_WINDOW, VOL_WINDOW, classify_series

REGIME_VERSION = "v1"                    # pins atlas/dcp/signals/regime/v1.py
REGIME_BENCHMARK = "SPY"
REGIME_LOOKBACK_BARS = 252               # fixed input window (module docstring)
_MIN_REGIME_BARS = max(TREND_WINDOW, VOL_WINDOW + 1) + 1  # first post-warmup label

_VERSION_LITERAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def extract_regime_evidence(session: Session, *, on: date) -> tuple[str, str] | None:
    """(ref, body) with the SPY regime label as of the last SPY vendor bar at
    or before `on`, or None when SPY history is too thin for a post-warmup
    label (or absent entirely)."""
    rows = session.execute(text(
        "SELECT pb.bar_date, pb.close FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol = :sym AND pb.source = 'EodhdAdapter' "
        "  AND pb.bar_date <= :on ORDER BY pb.bar_date DESC LIMIT :n"),
        {"sym": REGIME_BENCHMARK, "on": on, "n": REGIME_LOOKBACK_BARS}).all()
    if len(rows) < _MIN_REGIME_BARS:
        return None                      # warmup label would be a coverage artifact
    rows = list(reversed(rows))
    as_of: date = rows[-1].bar_date
    splits = [Split(symbol=REGIME_BENCHMARK, action_date=r.action_date,
                    ratio=Decimal(r.ratio))
              for r in session.execute(text(
                  "SELECT ca.action_date, ca.ratio FROM market.corporate_actions ca "
                  "JOIN market.instruments i ON i.id = ca.instrument_id "
                  "WHERE i.symbol = :sym AND ca.action_type = 'split' "
                  "  AND ca.action_date <= :on ORDER BY ca.action_date"),
                  {"sym": REGIME_BENCHMARK, "on": as_of}).all()]
    bars = [Bar(symbol=REGIME_BENCHMARK, bar_date=r.bar_date, open=r.close,
                high=r.close, low=r.close, close=r.close, volume=0) for r in rows]
    closes = [float(b.close) for b in adjust_for_splits(bars, splits)]
    label = classify_series([OBar(open=c, high=c, low=c, close=c, volume=0.0)
                             for c in closes])[-1]
    return (f"dcp:regime:{REGIME_VERSION}:{REGIME_BENCHMARK}:{as_of.isoformat()}",
            f"Market regime (deterministic classifier {REGIME_VERSION}, "
            f"{REGIME_BENCHMARK} benchmark): {label} as of {as_of.isoformat()}.")


def _plain(value: object) -> str | None:
    """A payload number as a plain decimal literal, or None (fail closed).
    jsonb cannot carry NaN/inf, but the check costs nothing and this module
    must never render anything that is not a plain decimal literal."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return format(Decimal(str(value)), "f")


def _components_clause(entry: dict[str, Any]) -> str | None:
    score = _plain(entry.get("score"))
    ret20 = _plain(entry.get("ret20_abs"))
    ret20_rank = _plain(entry.get("ret20_rank"))
    surge = _plain(entry.get("volume_surge"))
    surge_rank = _plain(entry.get("surge_rank"))
    if None in (score, ret20, ret20_rank, surge, surge_rank):
        return None
    return (f"attention score {score}, from 20-session absolute return {ret20} "
            f"(cross-sectional rank {ret20_rank}) and volume surge {surge} "
            f"(rank {surge_rank})")


def extract_scanner_context(session: Session, symbol: str, *,
                            on: date) -> tuple[str, str] | None:
    """(ref, body) from the latest scanner.completed audit payload that both
    shortlists `symbol` and covers the session `on` for the symbol's market;
    None otherwise (analyze-box runs, stale scans, unlisted symbols)."""
    market = session.execute(text(
        "SELECT market FROM market.instruments WHERE symbol = :sym"),
        {"sym": symbol}).scalar()
    if market is None:
        return None
    payload: dict[str, Any] | None = session.execute(text(
        "SELECT e.payload FROM audit.decision_events e "
        "WHERE e.event_type = 'scanner.completed' "
        "  AND e.payload->'sessions'->>:mkt = :on "
        "  AND e.payload->'shortlist' @> CAST(:probe AS jsonb) "
        "ORDER BY e.seq DESC LIMIT 1"),
        {"mkt": market, "on": on.isoformat(),
         "probe": json.dumps([{"symbol": symbol}])}).scalar()
    if payload is None:
        return None
    entry = next((e for e in payload.get("shortlist", [])
                  if isinstance(e, dict) and e.get("symbol") == symbol), None)
    version = payload.get("criteria_version")
    scanned, eligible = payload.get("scanned"), payload.get("eligible")
    if (entry is None or not isinstance(version, str)
            or not _VERSION_LITERAL.fullmatch(version)
            or not isinstance(scanned, int) or isinstance(scanned, bool)
            or not isinstance(eligible, int) or isinstance(eligible, bool)):
        return None                      # legacy/malformed payload: fail closed
    components = _components_clause(entry)
    if entry.get("held") is True:
        why = ("on the shortlist as a held/book name"
               + (f", with {components}" if components else
                  " (no attention score today)"))
    elif components is not None:
        why = f"shortlisted with {components}"
    else:
        return None                      # scored entry must carry its components
    body = (f"Scanner context for {symbol} (deterministic scanner, criteria "
            f"version {version} — attention, not prediction; scan of session "
            f"{on.isoformat()}): {why}. Scanned {scanned} instruments, "
            f"{eligible} eligible.")
    return f"dcp:scanner:{version}:{symbol}:{on.isoformat()}", body
