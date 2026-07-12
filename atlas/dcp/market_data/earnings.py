"""Earnings calendar: vendor ingest + the desk's ISO-dates-only evidence block.

Desk-review memo 2026-07 item 9: roughly one memo in three straddles an
earnings print, and the scanner's attention heuristic is biased toward
earnings-adjacent names without knowing it. This module gives the desk the
dates — and ONLY the dates.

SECURITY — ZERO INJECTION SURFACE. The evidence body rendered here contains
ISO dates and session counts computed by our own exchange calendar. No vendor
string is ever rendered: the vendor's timing flag is stored only when it
matches a closed vocabulary (models.EARNINGS_WHEN_TIMES, enforced at the
adapter boundary) and is deliberately NOT rendered in v1; actual/estimate/
surprise figures are not even stored (they are short-horizon signal the desk
has no validated use for, and every stored field is a field someone will one
day be tempted to render).

Refresh mechanics (nightly, wired into run_daily_ingest exactly like
fundamentals — fail-soft per instrument, counts in the report + audit
payload):

- Window: [today - PAST_WINDOW_DAYS, today + FUTURE_WINDOW_DAYS]. The memo
  asks for the next-30-days window; the fetch deliberately extends backwards
  as well, for two load-bearing reasons: (a) the evidence's "Last report
  YYYY-MM-DD." line needs a recorded past print — a future-only window would
  leave it permanently unrenderable; (b) an instrument whose next print is
  more than 30 days out gets an EMPTY future window, and without re-upserted
  past rows nothing would bump fetched_at, so the >STALE_DAYS throttle would
  degenerate to a nightly vendor call for most of the universe. 200 days
  covers semi-annual reporters, not just quarterly ones.
- Staleness: an instrument refreshes when it has no stored rows at all or
  its newest fetched_at date is more than STALE_DAYS days old. Instruments
  the vendor has NOTHING for anywhere in the window (ETFs — no earnings,
  ever) have no rows to timestamp and are re-checked nightly: a handful of
  cheap calls, honestly spent rather than a fabricated marker row.
- Supersede-on-refresh: future entries are vendor FORECASTS. A successful
  refresh DELETEs this instrument's rows with report_date >= today that the
  vendor no longer reports (a rescheduled print must never linger as a
  phantom), then upserts the fetched set (ON CONFLICT on the natural key
  updates when_time/fetched_at/source). Rows strictly in the past are
  recorded facts and are never deleted here.
- fetched_at comes from the injected clock (CLAUDE.md invariant 6).

Evidence semantics: `on` is the evidence date (the symbol's last bar date).
"Next" is the earliest stored report_date strictly after `on`; "last" is the
latest at or before `on`; the session count uses the instrument's own
exchange calendar. No fetched_at <= on filter is applied: scheduled dates
are known ahead of time and the desk runs live — the ref pins the fetch date
so the memo's provenance (research.memo_evidence) records exactly which
calendar state was argued from. None = nothing on record; the desk keeps its
current evidence set rather than fabricating a line.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.models import EarningsEvent

ROOT = Path(__file__).resolve().parents[3]

STALE_DAYS = 3            # newest fetched_at older than this -> refresh tonight
FUTURE_WINDOW_DAYS = 30   # the memo's next-30-days freshness objective
PAST_WINDOW_DAYS = 200    # backwards reach: "last report" data + staleness bump


@dataclass(frozen=True)
class EarningsDaily:
    fetched: tuple[str, ...]  # refreshed this run (stale or no rows before)
    fresh: tuple[str, ...]    # skipped: newest fetched_at within STALE_DAYS
    failed: tuple[str, ...]   # vendor failure (also recorded in report.failures)


def upsert_earnings(session: Session, instrument_id: object, event: EarningsEvent,
                    fetched_at: datetime, source: str) -> None:
    """Natural-key upsert: a re-fetched date refreshes when_time/fetched_at —
    this table records the vendor's CURRENT calendar, not snapshots."""
    session.execute(text(
        "INSERT INTO market.earnings_calendar "
        "(instrument_id, report_date, when_time, fetched_at, source) "
        "VALUES (:iid, :d, :w, :fa, :src) "
        "ON CONFLICT (instrument_id, report_date) DO UPDATE SET "
        "  when_time=:w, fetched_at=:fa, source=:src"),
        {"iid": instrument_id, "d": event.report_date, "w": event.when_time,
         "fa": fetched_at, "src": source})


def refresh_earnings(session: Session, adapter: MarketDataAdapter,
                     now: datetime, failures: list[str]) -> EarningsDaily:
    """One calendar refresh per ACTIVE instrument whose stored calendar is
    stale or absent (all markets — the vendor endpoint has no exchange
    calendar). Fail-soft per instrument: a vendor failure is recorded in
    `failures` (alertable, exit 2 upstream) and the run continues."""
    source = type(adapter).__name__
    today = now.astimezone(UTC).date()
    start = today - timedelta(days=PAST_WINDOW_DAYS)
    end = today + timedelta(days=FUTURE_WINDOW_DAYS)
    rows = session.execute(text(
        "SELECT i.id, i.symbol, "
        "       (SELECT max(ec.fetched_at) FROM market.earnings_calendar ec "
        "        WHERE ec.instrument_id = i.id) AS latest "
        "FROM market.instruments i WHERE i.is_active "
        "ORDER BY i.symbol")).mappings().all()
    fetched: list[str] = []
    fresh: list[str] = []
    failed: list[str] = []
    for inst in rows:
        latest: datetime | None = inst["latest"]
        if (latest is not None
                and (today - latest.astimezone(UTC).date()).days <= STALE_DAYS):
            fresh.append(inst["symbol"])
            continue
        try:
            events = adapter.fetch_earnings_calendar(inst["symbol"], start, end)
        except Exception as exc:  # vendor failure: report + exit 2, refresh the rest
            failures.append(f"earnings {inst['symbol']}: vendor fetch failed: {exc}")
            failed.append(inst["symbol"])
            continue
        # supersede: forecasts the vendor no longer reports are deleted, never
        # left as phantom prints; past rows are facts and stay untouched
        keep = [e.report_date for e in events if e.report_date >= today]
        if keep:
            session.execute(text(
                "DELETE FROM market.earnings_calendar "
                "WHERE instrument_id = :iid AND report_date >= :today "
                "  AND NOT (report_date = ANY(:keep))"),
                {"iid": inst["id"], "today": today, "keep": keep})
        else:  # empty ANY() arrays cannot type-infer; make the branch explicit
            session.execute(text(
                "DELETE FROM market.earnings_calendar "
                "WHERE instrument_id = :iid AND report_date >= :today"),
                {"iid": inst["id"], "today": today})
        for event in events:
            upsert_earnings(session, inst["id"], event, now, source)
        fetched.append(inst["symbol"])
    return EarningsDaily(fetched=tuple(fetched), fresh=tuple(fresh),
                         failed=tuple(failed))


def render_earnings_body(symbol: str, on: date, next_report: date | None,
                         next_sessions: int | None, last_report: date | None) -> str:
    """Pure render, golden-pinned in the tests: ISO dates and session counts
    only. Every number is a standalone token so a memo quoting it grounds
    verbatim (atlas/agents/runtime/grounding.py)."""
    if next_report is not None:
        assert next_sessions is not None
        plural = "" if next_sessions == 1 else "s"
        head = (f"Earnings calendar for {symbol}: next scheduled report "
                f"{next_report.isoformat()} ({next_sessions} session{plural} "
                f"after {on.isoformat()}).")
        if last_report is not None:
            return head + f" Last report {last_report.isoformat()}."
        return head + " No earlier report on record."
    assert last_report is not None
    return (f"Earnings calendar for {symbol}: no scheduled report on record "
            f"after {on.isoformat()}. Last report {last_report.isoformat()}.")


def extract_earnings_evidence(session: Session, symbol: str, *,
                              on: date) -> tuple[str, str] | None:
    """(ref, body) from the stored calendar around the evidence date `on`, or
    None when nothing is on record for the symbol — the desk keeps its
    current evidence set; a fabricated earnings line is never an option."""
    row = session.execute(text(
        "SELECT i.market, "
        "  (SELECT min(ec.report_date) FROM market.earnings_calendar ec "
        "   WHERE ec.instrument_id = i.id AND ec.report_date > :on) AS next, "
        "  (SELECT max(ec.report_date) FROM market.earnings_calendar ec "
        "   WHERE ec.instrument_id = i.id AND ec.report_date <= :on) AS last, "
        "  (SELECT max(ec.fetched_at) FROM market.earnings_calendar ec "
        "   WHERE ec.instrument_id = i.id) AS fetched "
        "FROM market.instruments i WHERE i.symbol = :sym"),
        {"sym": symbol, "on": on}).mappings().first()
    if row is None or (row["next"] is None and row["last"] is None):
        return None
    next_report: date | None = row["next"]
    sessions = (len(trading_days_between(row["market"], on + timedelta(days=1),
                                         next_report))
                if next_report is not None else None)
    fetched: datetime = row["fetched"]
    ref = f"dcp:earnings:{symbol}:{fetched.astimezone(UTC).date().isoformat()}"
    return ref, render_earnings_body(symbol, on, next_report, sessions, row["last"])


def main() -> None:
    """Deliberate one-off/operator run against the configured database: the
    same refresh the nightly ingest wires in, with its own audit event and a
    coverage summary. Exit 2 on any per-instrument vendor failure."""
    from atlas.core.audit_repo import PostgresAuditLog
    from atlas.core.clock import Clock, FrozenClock, SystemClock
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters import adapter_from_settings

    p = argparse.ArgumentParser(description="Earnings-calendar refresh for every "
                                            "active instrument (stale > "
                                            f"{STALE_DAYS} days)")
    p.add_argument("--now", help="aware ISO datetime pinning the clock for "
                                 "deterministic re-runs")
    a = p.parse_args()
    clock: Clock = FrozenClock(datetime.fromisoformat(a.now)) if a.now else SystemClock()
    adapter = adapter_from_settings(fixtures_root=ROOT / "tests" / "fixtures",
                                    seeds_csv=ROOT / "seeds" / "instruments_seed.csv")
    failures: list[str] = []
    with session_scope() as s:
        report = refresh_earnings(s, adapter, clock.now(), failures)
        today = clock.now().astimezone(UTC).date()
        coverage = s.execute(text(
            "SELECT count(*) FILTER (WHERE up.n > 0) AS with_upcoming, "
            "       count(*) FILTER (WHERE up.n = 0 AND past.n > 0) AS past_only, "
            "       count(*) FILTER (WHERE up.n = 0 AND past.n = 0) AS empty "
            "FROM market.instruments i, LATERAL ("
            "  SELECT count(*) AS n FROM market.earnings_calendar ec "
            "  WHERE ec.instrument_id = i.id AND ec.report_date > :today) up, "
            "LATERAL ("
            "  SELECT count(*) AS n FROM market.earnings_calendar ec "
            "  WHERE ec.instrument_id = i.id AND ec.report_date <= :today) past "
            "WHERE i.is_active"), {"today": today}).mappings().one()
        PostgresAuditLog(s, clock).append(
            event_type="market.earnings_ingest.completed", entity_type="market",
            entity_id=today.isoformat(), actor_type="scheduler",
            actor_id="earnings_ingest",
            payload={"now": clock.now().isoformat(),
                     "fetched": list(report.fetched), "fresh": list(report.fresh),
                     "failed": list(report.failed), "failures": failures,
                     "coverage": {"with_upcoming": int(coverage["with_upcoming"]),
                                  "past_only": int(coverage["past_only"]),
                                  "empty": int(coverage["empty"])}})
    print(f"earnings: {len(report.fetched)} fetched, {len(report.fresh)} fresh, "
          f"{len(report.failed)} failed"
          + (f" ({list(report.failed)})" if report.failed else ""))
    print(f"coverage (active instruments): {coverage['with_upcoming']} with an "
          f"upcoming report, {coverage['past_only']} past-only, "
          f"{coverage['empty']} with nothing on record")
    for msg in failures:
        print(f"FAILURE: {msg}")
    raise SystemExit(2 if failures else 0)


if __name__ == "__main__":
    main()
