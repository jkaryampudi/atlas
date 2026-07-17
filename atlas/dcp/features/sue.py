"""sue_foster_olsen_shevlin — PEAD's standardized unexpected earnings as a
stored point-in-time feature, byte-equal to what signals/pead's live() serves.

THE MATH IS IMPORTED, NOT REWRITTEN (ADR-0011 step 1): compute_sue_reports
(the CORRECTED no-double-adjust SUE series), effective_index mapping and the
EarningsView staleness/no-fallback rules all come from signals/pead/v1, whose
source file is part of this feature's code_sha.

REPRESENTATION — DENSE CARRY-FORWARD (the documented choice). pead's live(t)
is not "the SUE stamped on a report": it is "the most recent report knowable
by t, IF fresh (<= 63 sessions) and IF its SUE is defined — with NO fallback
to an older report". A sparse per-report representation cannot reproduce that
under the store's generic read (a newer report with UNDEFINED SUE must shadow
an older defined one into None — a latest-row-<= read would wrongly fall
back). So materialization evaluates live(t) at EVERY target session and
stores a row exactly where it is defined; a session where live(t) is None
gets NO row. Combined with the store's carry-0 as-of read, feature_at(on)
returns exactly live(t) at the corresponding session — including the
staleness expiry and the newer-undefined-report shadow — which the
equivalence tests pin dense, session by session.

NO LOOK-AHEAD, structurally: reports enter the view only through their
EFFECTIVE panel index (BeforeMarket -> the report session's own close;
AfterMarket/unknown -> the next session), and live(t) physically cannot read
an event with effective_index > t — v1's own guarantee. The query cap at
report_date <= END is therefore safe for every t <= END: a report dated
after t maps past t and is invisible at t.

THE PANEL CALENDAR spans _CAL_SLACK_DAYS (250) calendar days (~170 US
sessions) before the earliest target session — comfortably past the
63-session staleness window, so (a) every live-eligible report lands on its
exact panel index and (b) a report older than the calendar maps to index 0
and is correctly stale (same reasoning as signals/pead/generate).

TRADABILITY IS NOT PART OF THE FEATURE. pead's production ranker additionally
requires a vendor close at t (a name it can trade); the FEATURE is the
signal value itself, so it is stored whether or not the session has a bar.
Equivalence against the ranker is asserted on its own priced universe.

DATASET_VERSION EXTENT (see store.py for the hash): per symbol, over
market.earnings_surprises rows with report_date <= END — min(report_date),
max(report_date), row count. Reports dated after END never enter the extent;
any new/backfilled report <= END re-versions the dataset.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.earnings_history import EarningsSurprise
from atlas.dcp.signals.pead.v1 import build_earnings_view

VARIANT = "sue"                # primary signal (surprise_pct is a cross-check)
_CAL_SLACK_DAYS = 250          # ~170 US sessions >> STALENESS_SESSIONS (63)


def compute_sue(db: Session, symbol: str, instrument_id: UUID,
                sessions: list[date]) -> dict[date, float]:
    """{session: live SUE} for every target session where pead's live(t) is
    defined (dense carry-forward — module docstring). Sessions that are not
    US trading days are skipped."""
    if not sessions:
        return {}
    ordered = sorted(sessions)
    start, end = ordered[0], ordered[-1]
    cal = trading_days_between("US", start - timedelta(days=_CAL_SLACK_DAYS),
                               end)
    if not cal:
        return {}
    index_of = {d: i for i, d in enumerate(cal)}

    reports: list[EarningsSurprise] = [
        EarningsSurprise(
            symbol=symbol, fiscal_period_end=r.fiscal_period_end,
            report_date=r.report_date, eps_actual=Decimal(r.eps_actual),
            eps_estimate=Decimal(r.eps_estimate),
            surprise_pct=(Decimal(r.surprise_pct)
                          if r.surprise_pct is not None else None),
            before_after_market=r.before_after_market, currency=None)
        for r in db.execute(text(
            "SELECT fiscal_period_end, report_date, eps_actual, eps_estimate, "
            "       surprise_pct, before_after_market "
            "FROM market.earnings_surprises "
            "WHERE instrument_id = :iid AND report_date <= :end "
            "ORDER BY fiscal_period_end"),
            {"iid": instrument_id, "end": end})]
    if not reports:
        return {}
    view = build_earnings_view({symbol: reports}, cal)

    out: dict[date, float] = {}
    for t in ordered:
        i = index_of.get(t)
        if i is None:
            continue                    # not a US session
        val = view.live(symbol, i, variant=VARIANT)
        if val is not None:
            out[t] = val
    return out


def sue_extent(db: Session, symbols: list[str],
               end: date) -> dict[str, object]:
    """The input-data extent hashed into dataset_version (module docstring)."""
    per_symbol: dict[str, object] = {}
    for symbol in symbols:
        row = db.execute(text(
            "SELECT min(es.report_date) AS lo, max(es.report_date) AS hi, "
            "       count(*) AS n "
            "FROM market.earnings_surprises es "
            "JOIN market.instruments i ON i.id = es.instrument_id "
            "WHERE i.symbol = :s AND es.report_date <= :end"),
            {"s": symbol, "end": end}).one()
        per_symbol[symbol] = {
            "earnings": {"min": row.lo, "max": row.hi, "rows": int(row.n)}}
    return {"symbols": per_symbol}
