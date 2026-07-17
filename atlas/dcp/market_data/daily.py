"""Nightly incremental ingest: bars + splits + FX + per-day quality gates.

Usage: python -m atlas.dcp.market_data.daily [--now 2026-07-11T22:00:00+00:00]
`--now` (aware ISO datetime) pins the clock for deterministic re-runs; without
it the real wall clock decides which sessions are complete. The scheduler
alerts on non-zero exit: 2 on any red gate, any missing weekday FX rate, or
any vendor failure. Running twice in a row is a no-op the second time.

Completed-session convention: a session may be requested/stored only once the
exchange calendar's UTC close is at or before the injected clock's now() — a
session still in progress would yield a partial bar (look-ahead poison). Each
ACTIVE instrument advances independently from its own latest stored bar, so
one instrument's lag never re-fetches the whole market. An instrument with NO
stored bars is never silently deep-backfilled here: it is reported as
needs_backfill (backfill is a separate, deliberate, 1y-capped operation per
ADR-0004) and, because gate coverage still expects it, every gated day goes
honestly RED until the human runs that backfill.

Gates are evaluated from STORED bars (not just this run's fetches): instrument
windows differ, and a freshly fetched bar must be judged alongside neighbours
already in the database. Non-trading days after the last completed session get
the existing carry-forward gate, never a false green.

FX extends every pair already present in market.fx_rates_daily from its latest
stored rate through the last completed weekday (EODHD FOREX rates are final at
22:00 UTC). A weekday gap is a failure — FOREX holidays are rare enough that a
human should look. A required pair with no stored rates at all is a failure,
not a silent skip.

Fundamentals refresh (after bars/FX): every ACTIVE instrument whose latest
market.fundamentals snapshot is older than FUNDAMENTALS_STALE_DAYS (or absent)
gets ONE new snapshot row — the raw vendor document, whole, as_of = the
clock's UTC date. Snapshots are append-style (ON CONFLICT DO NOTHING): a
stored payload is never updated. A per-instrument vendor failure is recorded
in report.failures and the run continues (fail-soft, like bars). NOTE: the
payload contains vendor free-text — a prompt-injection surface; only the
whitelist extractor in fundamentals.py may turn it into agent evidence.

Earnings-calendar refresh (after fundamentals): the same shape — every ACTIVE
instrument whose stored calendar is stale (> earnings.STALE_DAYS) or absent
gets one window refresh, fail-soft per instrument, counts in the report and
the audit payload. Mechanics (window, supersede-on-refresh, closed-vocabulary
timing flag) live in market_data/earnings.py.

Estimate-snapshot step (LAST, after earnings): the ADR-0011 forward archive —
one append-only Earnings::Trend consensus snapshot per ACTIVE US single name
per session, DAILY cadence (the vendor overwrites this block in place, so a
missed day is lost forever — deliberately not on the fundamentals step's
weekly staleness throttle, which is also why it re-fetches the document the
fundamentals step may have fetched tonight). Gated by the once-daily guard
(a cycle re-run/replay skips idempotently), fail-soft per instrument, counts
in the report and the audit payload. Running last + per-instrument fail-soft
means a vendor failure here can never affect bars/FX/fundamentals/earnings.
Mechanics live in market_data/estimate_snapshots.py.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.market_data.adapters.base import MarketDataAdapter
from atlas.dcp.market_data.calendars import (is_trading_day, last_completed_session,
                                              local_date, previous_trading_day,
                                              trading_days_between)
from atlas.dcp.market_data.earnings import EarningsDaily, refresh_earnings
from atlas.dcp.market_data.estimate_snapshots import (EstimateSnapshotDaily,
                                                       snapshot_estimates,
                                                       universe_symbols)
from atlas.dcp.market_data.fx import required_pairs, upsert_rate
from atlas.dcp.market_data.ingest import (_non_trading_day_gate, record_split,
                                          upsert_bar, write_gate)
from atlas.dcp.market_data.models import Bar, GateStatus
from atlas.dcp.market_data.quality import evaluate_gate, inception_map

ROOT = Path(__file__).resolve().parents[3]

FX_EOD_UTC = time(22, 0)  # EODHD FOREX end-of-day rolls at 22:00 UTC (5pm New York)

# a fundamentals snapshot this many days old (or younger) is still fresh;
# older (or absent) is refetched — weekly cadence with daily opportunity
FUNDAMENTALS_STALE_DAYS = 7


@dataclass(frozen=True)
class MarketDaily:
    market: str
    bars: int
    days: tuple[date, ...]               # sessions this run fetched (and is gating)
    gates: tuple[tuple[date, str], ...]  # every gate written, incl. carry-forward days
    needs_backfill: tuple[str, ...]      # active instruments with no stored bars at all

    @property
    def red(self) -> int:
        return sum(1 for _, status in self.gates if status == GateStatus.RED.value)


@dataclass(frozen=True)
class FxPairDaily:
    pair: str
    rows: int
    missing_weekdays: tuple[date, ...]  # weekday gap in the window — always a failure


@dataclass(frozen=True)
class FundamentalsDaily:
    fetched: tuple[str, ...]  # snapshot inserted this run (stale or absent before)
    fresh: tuple[str, ...]    # skipped: latest snapshot within FUNDAMENTALS_STALE_DAYS
    failed: tuple[str, ...]   # vendor failure (also recorded in report.failures)


@dataclass(frozen=True)
class DailyIngestReport:
    markets: dict[str, MarketDaily]
    fx: dict[str, FxPairDaily]
    fundamentals: FundamentalsDaily
    earnings: EarningsDaily
    estimates: EstimateSnapshotDaily  # ADR-0011 forward archive (daily, guarded)
    failures: tuple[str, ...]  # vendor failures + required FX pairs with no history

    @property
    def needs_backfill(self) -> tuple[str, ...]:
        return tuple(f"{m.market}:{sym}" for m in self.markets.values()
                     for sym in m.needs_backfill)

    @property
    def failed(self) -> bool:
        return (bool(self.failures)
                or any(m.red for m in self.markets.values())
                or any(f.missing_weekdays for f in self.fx.values()))


def incremental_sessions(market: str, latest: date, now: datetime) -> list[date]:
    """Sessions strictly after `latest` through the last completed session at
    `now` — the exact fetch window for one instrument. Empty when up to date,
    and never containing a session still in progress."""
    return trading_days_between(market, latest + timedelta(days=1),
                                last_completed_session(market, now))


def fx_last_completed_weekday(now: datetime) -> date:
    """Latest weekday whose FOREX end-of-day (22:00 UTC) is at or before `now`."""
    if now.tzinfo is None:
        raise ValueError("fx_last_completed_weekday requires an aware datetime")
    d = now.astimezone(UTC).date()
    while d.weekday() >= 5 or datetime.combine(d, FX_EOD_UTC, tzinfo=UTC) > now:
        d -= timedelta(days=1)
    return d


def _stored_bars(session: Session, market: str, start: date,
                 end: date) -> dict[date, list[Bar]]:
    rows = session.execute(text(
        "SELECT i.symbol, pb.bar_date, pb.open, pb.high, pb.low, pb.close, pb.volume "
        "FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.market = :m AND i.is_active AND pb.bar_date BETWEEN :a AND :b"),
        {"m": market, "a": start, "b": end}).mappings()
    out: dict[date, list[Bar]] = defaultdict(list)
    for r in rows:
        if any(r[f] is None for f in ("open", "high", "low", "close", "volume")):
            continue  # fail closed: an incomplete stored row counts as MISSING
            #           for its symbol, so per-instrument coverage reds the day
            #           — never crash the whole run on one degraded row
        out[r["bar_date"]].append(Bar(
            symbol=r["symbol"], bar_date=r["bar_date"],
            open=Decimal(r["open"]), high=Decimal(r["high"]), low=Decimal(r["low"]),
            close=Decimal(r["close"]), volume=int(r["volume"])))
    return out


def _stored_splits(session: Session, market: str, start: date,
                   end: date) -> dict[date, set[str]]:
    rows = session.execute(text(
        "SELECT i.symbol, ca.action_date FROM market.corporate_actions ca "
        "JOIN market.instruments i ON i.id = ca.instrument_id "
        "WHERE i.market = :m AND ca.action_date BETWEEN :a AND :b"),
        {"m": market, "a": start, "b": end}).mappings()
    out: dict[date, set[str]] = defaultdict(set)
    for r in rows:
        out[r["action_date"]].add(r["symbol"])
    return out


def _gate_market(session: Session, market: str, days: list[date],
                 expected_symbols: frozenset[str], last_completed: date,
                 now: datetime) -> list[tuple[date, str]]:
    """Gate every fetched session (from stored bars, window = previous session +
    day, exactly as backfill gates) plus carry-forward gates for non-trading
    days after the last completed session up to the exchange-local today.

    Rules v1.2: expected symbols are inception-filtered (a symbol is expected
    only from its earliest STORED bar onward — quality.inception_map). For the
    incremental path this is almost always a no-op (every gated day is recent),
    but it keeps the three gate call sites on one rule set; an instrument with
    NO stored bars stays fail-closed expected, so needs_backfill days still go
    honestly RED."""
    written: list[tuple[date, str]] = []
    if days:
        lo = previous_trading_day(market, days[0])
        bars_by_day = _stored_bars(session, market, lo, days[-1])
        explained_by_day = _stored_splits(session, market, lo, days[-1])
        inceptions = inception_map(session, market)
        for d in days:
            p = previous_trading_day(market, d)
            window = {p: bars_by_day.get(p, []), d: bars_by_day.get(d, [])}
            gate = evaluate_gate(market=market, as_of=d, expected_days=[d],
                                 bars_by_day=window,
                                 explained_symbols=frozenset(explained_by_day.get(d, set())),
                                 expected_symbols=expected_symbols,
                                 inceptions=inceptions)
            write_gate(session, gate)
            written.append((d, gate.status.value))
    d = last_completed + timedelta(days=1)
    horizon = local_date(market, now)
    while d <= horizon and not is_trading_day(market, d):
        gate = _non_trading_day_gate(session, market, d)
        write_gate(session, gate)
        written.append((d, gate.status.value))
        d += timedelta(days=1)
    return written


def _ingest_market(session: Session, adapter: MarketDataAdapter, market: str,
                   now: datetime, failures: list[str]) -> MarketDaily:
    source = type(adapter).__name__
    last_completed = last_completed_session(market, now)
    instruments = session.execute(text(
        "SELECT i.id, i.symbol, "
        "       (SELECT max(pb.bar_date) FROM market.price_bars_daily pb "
        "        WHERE pb.instrument_id = i.id) AS latest "
        "FROM market.instruments i WHERE i.market = :m AND i.is_active "
        "ORDER BY i.symbol"), {"m": market}).mappings().all()

    needs_backfill: list[str] = []
    covered: set[date] = set()
    n_bars = 0
    for inst in instruments:
        if inst["latest"] is None:
            needs_backfill.append(inst["symbol"])
            continue
        days = incremental_sessions(market, inst["latest"], now)
        if not days:
            continue
        try:
            splits = adapter.fetch_splits(inst["symbol"], days[0], days[-1])
            bars = adapter.fetch_bars(inst["symbol"], days[0], days[-1])
        except Exception as exc:  # vendor failure: report + exit 2, gate the rest
            failures.append(f"{market}:{inst['symbol']}: vendor fetch failed: {exc}")
            continue
        for sp in splits:
            record_split(session, inst["id"], sp, source)
        for b in bars:
            if not days[0] <= b.bar_date <= days[-1]:
                continue  # never store a bar outside the completed window
            upsert_bar(session, inst["id"], b, source)
            n_bars += 1
        covered.update(days)

    gates = _gate_market(session, market, sorted(covered),
                         frozenset(i["symbol"] for i in instruments),
                         last_completed, now)
    return MarketDaily(market=market, bars=n_bars, days=tuple(sorted(covered)),
                       gates=tuple(gates), needs_backfill=tuple(needs_backfill))


def _weekdays(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _ingest_fx(session: Session, adapter: MarketDataAdapter, now: datetime,
               failures: list[str]) -> dict[str, FxPairDaily]:
    source = type(adapter).__name__
    end = fx_last_completed_weekday(now)
    stored = session.execute(text(
        "SELECT base, quote, max(rate_date) AS latest FROM market.fx_rates_daily "
        "GROUP BY base, quote ORDER BY base, quote")).mappings().all()
    have = {(p["base"], p["quote"]) for p in stored}
    for req in required_pairs(session):
        if req not in have:
            failures.append(f"fx {req[0]}{req[1]}: no stored rates at all — "
                            "run the deliberate backfill first (ADR-0004)")

    out: dict[str, FxPairDaily] = {}
    for p in stored:
        base, quote = p["base"], p["quote"]
        pair, start = f"{base}{quote}", p["latest"] + timedelta(days=1)
        if start > end:
            out[pair] = FxPairDaily(pair=pair, rows=0, missing_weekdays=())
            continue
        try:
            series = adapter.fetch_fx_series(base, quote, start, end)
        except Exception as exc:  # vendor failure: report + exit 2
            failures.append(f"fx {pair}: vendor fetch failed: {exc}")
            continue
        rows = 0
        for d, rate in sorted(series.items()):
            if start <= d <= end:  # never store a rate for an incomplete day
                upsert_rate(session, base=base, quote=quote, day=d, rate=rate,
                            source=source)
                rows += 1
        out[pair] = FxPairDaily(pair=pair, rows=rows,
                                missing_weekdays=tuple(d for d in _weekdays(start, end)
                                                       if d not in series))
    return out


def _refresh_fundamentals(session: Session, adapter: MarketDataAdapter,
                          now: datetime, failures: list[str]) -> FundamentalsDaily:
    """One append-style snapshot per ACTIVE instrument whose latest snapshot
    is stale or absent (all markets, like FX — fundamentals have no exchange
    calendar). as_of is the injected clock's UTC date, so a same-day re-run
    finds every refreshed instrument fresh and is a no-op; ON CONFLICT DO
    NOTHING keeps even a racing re-run append-only, never an update."""
    source = type(adapter).__name__
    today = now.astimezone(UTC).date()
    rows = session.execute(text(
        "SELECT i.id, i.symbol, "
        "       (SELECT max(f.as_of) FROM market.fundamentals f "
        "        WHERE f.instrument_id = i.id) AS latest "
        "FROM market.instruments i WHERE i.is_active "
        "ORDER BY i.symbol")).mappings().all()
    fetched: list[str] = []
    fresh: list[str] = []
    failed: list[str] = []
    for inst in rows:
        if (inst["latest"] is not None
                and (today - inst["latest"]).days <= FUNDAMENTALS_STALE_DAYS):
            fresh.append(inst["symbol"])
            continue
        try:
            payload = adapter.fetch_fundamentals(inst["symbol"])
        except Exception as exc:  # vendor failure: report + exit 2, refresh the rest
            failures.append(f"fundamentals {inst['symbol']}: vendor fetch failed: {exc}")
            failed.append(inst["symbol"])
            continue
        session.execute(text(
            "INSERT INTO market.fundamentals (instrument_id, as_of, payload, source) "
            "VALUES (:iid, :d, CAST(:p AS jsonb), :src) "
            "ON CONFLICT (instrument_id, as_of) DO NOTHING"),
            {"iid": inst["id"], "d": today, "p": json.dumps(payload), "src": source})
        fetched.append(inst["symbol"])
    return FundamentalsDaily(fetched=tuple(fetched), fresh=tuple(fresh),
                             failed=tuple(failed))


def run_daily_ingest(session: Session, clock: Clock, adapter: MarketDataAdapter, *,
                     markets: tuple[str, ...] = ("US", "AU")) -> DailyIngestReport:
    now = clock.now()
    failures: list[str] = []
    results = {m: _ingest_market(session, adapter, m, now, failures) for m in markets}
    fx = _ingest_fx(session, adapter, now, failures)
    fundamentals = _refresh_fundamentals(session, adapter, now, failures)
    earnings = refresh_earnings(session, adapter, now, failures)
    # ADR-0011 forward archive: last, once-daily-guarded, fail-soft — a vendor
    # failure here is counted and alertable but cannot touch the steps above
    estimates = snapshot_estimates(session, adapter, universe_symbols(session),
                                   now=now, failures=failures, once_daily=True)

    report = DailyIngestReport(markets=results, fx=fx, fundamentals=fundamentals,
                               earnings=earnings, estimates=estimates,
                               failures=tuple(failures))
    PostgresAuditLog(session, clock).append(
        event_type="market.daily_ingest.completed", entity_type="market",
        entity_id=",".join(markets), actor_type="scheduler", actor_id="daily_ingest",
        payload={"now": now.isoformat(), "failed": report.failed,
                 "failures": list(report.failures),
                 "markets": {m: {"bars": r.bars,
                                 "days": [d.isoformat() for d in r.days],
                                 "gates": {d.isoformat(): s for d, s in r.gates},
                                 "needs_backfill": list(r.needs_backfill)}
                             for m, r in results.items()},
                 "fx": {pair: {"rows": f.rows,
                               "missing_weekdays": [d.isoformat()
                                                    for d in f.missing_weekdays]}
                        for pair, f in fx.items()},
                 "fundamentals": {"fetched": list(fundamentals.fetched),
                                  "fresh": list(fundamentals.fresh),
                                  "failed": list(fundamentals.failed)},
                 "earnings": {"fetched": list(earnings.fetched),
                              "fresh": list(earnings.fresh),
                              "failed": list(earnings.failed)},
                 "estimates": {"skipped": estimates.skipped,
                               "fetched": list(estimates.fetched),
                               "empty": list(estimates.empty),
                               "failed": list(estimates.failed),
                               "rows_stored": estimates.stored}})
    return report


def main() -> None:
    from atlas.core.clock import FrozenClock, SystemClock
    from atlas.core.db import session_scope
    from atlas.dcp.market_data.adapters import adapter_from_settings

    p = argparse.ArgumentParser(description="Nightly incremental ingest: bars, splits, "
                                            "FX and quality gates since the last stored bar")
    p.add_argument("--market", action="append", dest="markets",
                   help="restrict to a market (repeatable); default: US and AU")
    p.add_argument("--now", help="aware ISO datetime pinning the clock for deterministic "
                                 "re-runs (e.g. 2026-07-11T22:00:00+00:00)")
    a = p.parse_args()

    clock: Clock = FrozenClock(datetime.fromisoformat(a.now)) if a.now else SystemClock()
    markets = tuple(a.markets) if a.markets else ("US", "AU")
    adapter = adapter_from_settings(fixtures_root=ROOT / "tests" / "fixtures",
                                    seeds_csv=ROOT / "seeds" / "instruments_seed.csv")
    with session_scope() as s:
        report = run_daily_ingest(s, clock, adapter, markets=markets)

    for m, r in report.markets.items():
        line = (f"{m}: {r.bars} bars over {len(r.days)} session(s); gates: "
                + (", ".join(f"{d}={status}" for d, status in r.gates) or "none"))
        if r.needs_backfill:
            line += f"; NEEDS BACKFILL: {list(r.needs_backfill)}"
        print(line)
    for pair, f in report.fx.items():
        print(f"fx {pair}: {f.rows} row(s)"
              + (f", MISSING WEEKDAYS: {[d.isoformat() for d in f.missing_weekdays]}"
                 if f.missing_weekdays else ""))
    fnd = report.fundamentals
    print(f"fundamentals: {len(fnd.fetched)} fetched, {len(fnd.fresh)} fresh, "
          f"{len(fnd.failed)} failed"
          + (f" ({list(fnd.failed)})" if fnd.failed else ""))
    earn = report.earnings
    print(f"earnings: {len(earn.fetched)} fetched, {len(earn.fresh)} fresh, "
          f"{len(earn.failed)} failed"
          + (f" ({list(earn.failed)})" if earn.failed else ""))
    est = report.estimates
    if est.skipped:
        print("estimates: session already snapshot — once-daily guard skipped")
    else:
        print(f"estimates: {len(est.fetched)} fetched ({est.stored} new rows), "
              f"{len(est.empty)} empty, {len(est.failed)} failed"
              + (f" ({list(est.failed)})" if est.failed else ""))
    for msg in report.failures:
        print(f"FAILURE: {msg}")
    print("daily ingest via " + type(adapter).__name__ + ": "
          + ("FAILURES PRESENT — inspect market.data_quality_gates"
             if report.failed else "all green"))
    raise SystemExit(2 if report.failed else 0)


if __name__ == "__main__":
    main()
