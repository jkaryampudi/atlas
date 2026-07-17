"""Estimate-snapshot forward archive (ADR-0011): append-only immutability
(re-run = no-op; a changed vendor value is a NEW row and the old row is
untouched), the once-daily guard, fail-soft + the audit event with counts,
and the research-only read APIs (latest/series, no row -> None)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.estimate_snapshots import (
    latest_snapshot,
    snapshot_estimates,
    snapshot_series,
    snapshot_with_audit,
    universe_symbols,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

NOW1 = datetime(2026, 7, 17, 2, 0, tzinfo=UTC)   # session 2026-07-17
NOW2 = datetime(2026, 7, 18, 2, 0, tzinfo=UTC)   # session 2026-07-18
DAY1, DAY2 = date(2026, 7, 17), date(2026, 7, 18)
FPE1, FPE2 = date(2026, 9, 30), date(2026, 12, 31)


def _period(avg: str, up7: str | None = "3") -> dict:
    return {"earningsEstimateAvg": avg, "earningsEstimateNumberOfAnalysts": "33",
            "revenueEstimateAvg": "109578690550.00", "epsTrendCurrent": avg,
            "epsTrend7daysAgo": "1.9428", "epsTrend30daysAgo": "1.9514",
            "epsRevisionsUpLast7days": up7, "epsRevisionsUpLast30days": "4",
            "epsRevisionsDownLast7days": None, "epsRevisionsDownLast30days": "1"}


def _doc(trend: dict) -> dict:
    return {"General": {"Code": "ZEST"}, "Earnings": {"Trend": trend}}


DOC_DAY1 = _doc({FPE1.isoformat(): _period("1.9404"),
                 FPE2.isoformat(): _period("2.4000"),
                 "2017-06-30": _period("9.99")})     # stale period: never stored
# next day the vendor overwrote the consensus in place — our archive must not
DOC_DAY2 = _doc({FPE1.isoformat(): _period("1.9500", up7="5"),
                 FPE2.isoformat(): _period("2.4000")})


class _FakeAdapter:
    """Serves docs per symbol, counts vendor calls, melts for BOOM."""
    def __init__(self, docs: dict[str, dict]) -> None:
        self._docs = docs
        self.calls: list[str] = []

    def fetch_fundamentals(self, symbol: str) -> dict:
        self.calls.append(symbol)
        if symbol == "BOOM":
            raise RuntimeError("vendor 500")
        return self._docs[symbol]


def _instrument(s, symbol: str, instrument_type: str = "stock",
                market: str = "US") -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES (:sym, 'XTEST', :mkt, :typ, :sym, 'USD') RETURNING id"),
        {"sym": symbol, "mkt": market, "typ": instrument_type}).scalar())


ROWS = ("SELECT es.fiscal_period_end, es.snapshot_date, es.eps_estimate_avg, "
        "       es.revisions_up_7d, es.revisions_down_7d, es.fetched_at, es.source "
        "FROM market.estimate_snapshots es "
        "JOIN market.instruments i ON i.id = es.instrument_id "
        "WHERE i.symbol = :sym ORDER BY es.snapshot_date, es.fiscal_period_end")


def test_snapshot_then_rerun_is_noop_then_changed_value_is_new_row(pg_session):
    s = pg_session
    _instrument(s, "ZEST")
    adapter = _FakeAdapter({"ZEST": DOC_DAY1})

    rep1 = snapshot_estimates(s, adapter, ["ZEST"], now=NOW1, failures=[],
                              once_daily=False)
    assert rep1.fetched == ("ZEST",) and rep1.stored == 2 and not rep1.skipped

    # append-only: a same-session re-run (guard off) stores ZERO new rows
    rep1b = snapshot_estimates(s, adapter, ["ZEST"], now=NOW1, failures=[],
                               once_daily=False)
    assert rep1b.stored == 0 and rep1b.fetched == ("ZEST",)

    # next session the vendor has overwritten FPE1's consensus in place;
    # the archive records a NEW row and day 1's row is byte-for-byte untouched
    adapter._docs["ZEST"] = DOC_DAY2
    rep2 = snapshot_estimates(s, adapter, ["ZEST"], now=NOW2, failures=[],
                              once_daily=False)
    assert rep2.stored == 2

    rows = s.execute(text(ROWS), {"sym": "ZEST"}).all()
    assert [(r.fiscal_period_end, r.snapshot_date) for r in rows] == [
        (FPE1, DAY1), (FPE2, DAY1), (FPE1, DAY2), (FPE2, DAY2)]
    d1, d2 = rows[0], rows[2]                      # FPE1 on day 1 vs day 2
    assert d1.eps_estimate_avg == Decimal("1.9404")   # preserved, not updated
    assert d1.revisions_up_7d == Decimal("3")
    assert d1.fetched_at == NOW1 and d1.source == "_FakeAdapter"
    assert d2.eps_estimate_avg == Decimal("1.9500")   # the overwrite, as a fact
    assert d2.revisions_up_7d == Decimal("5")
    assert d1.revisions_down_7d is None            # vendor null stored as NULL
    # the stale 2017 period never entered the archive
    assert all(r.fiscal_period_end.year > 2017
               for r in s.execute(text(ROWS), {"sym": "ZEST"}).all())


def test_once_daily_guard_skips_without_vendor_spend(pg_session):
    s = pg_session
    _instrument(s, "ZEST")
    _instrument(s, "ZES2")
    adapter = _FakeAdapter({"ZEST": DOC_DAY1, "ZES2": DOC_DAY1})

    rep1 = snapshot_estimates(s, adapter, ["ZEST"], now=NOW1, failures=[])
    assert rep1.stored == 2 and not rep1.skipped
    calls_after_first = len(adapter.calls)

    # second run in the SAME session: skipped idempotently, zero vendor calls —
    # and the guard is per-session, not per-instrument: a different symbol
    # list is NOT silently completed (the counts are the honest record)
    rep2 = snapshot_estimates(s, adapter, ["ZEST", "ZES2"], now=NOW1, failures=[])
    assert rep2.skipped
    assert rep2.fetched == () and rep2.stored == 0 and rep2.failed == ()
    assert len(adapter.calls) == calls_after_first
    n = s.execute(text("SELECT count(*) FROM market.estimate_snapshots")).scalar()
    assert n == 2

    # the NEXT session resumes everyone
    rep3 = snapshot_estimates(s, adapter, ["ZEST", "ZES2"], now=NOW2, failures=[])
    assert not rep3.skipped and rep3.fetched == ("ZEST", "ZES2")


def test_fail_soft_and_audit_counts(clean_audit):
    s = clean_audit
    for sym in ("ZEST", "ZEMP", "BOOM"):          # ZNON deliberately not seeded
        _instrument(s, sym)
    adapter = _FakeAdapter({"ZEST": DOC_DAY1, "ZEMP": _doc({})})

    failures: list[str] = []
    rep = snapshot_with_audit(s, adapter, ["ZEST", "ZEMP", "BOOM", "ZNON"],
                              clock=FrozenClock(NOW1), failures=failures)
    assert rep.fetched == ("ZEST",)
    assert rep.empty == ("ZEMP",)
    assert set(rep.failed) == {"BOOM", "ZNON"}    # vendor failure + missing row
    assert rep.stored == 2 and not rep.skipped
    assert failures == ["estimates BOOM: vendor fetch failed: vendor 500",
                        "estimates ZNON: no instrument row"]

    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.estimate_snapshot_ingest.completed' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload is not None
    assert payload["skipped"] is False
    assert payload["symbols"] == 4
    assert payload["fetched"] == ["ZEST"] and payload["empty"] == ["ZEMP"]
    assert set(payload["failed"]) == {"BOOM", "ZNON"}
    assert payload["rows_stored"] == 2 and len(payload["failures"]) == 2
    assert payload["coverage"] == {"instruments": 1, "sessions": 1, "rows": 2}

    # a guard-skipped run still leaves its honest audit trace
    rep2 = snapshot_with_audit(s, adapter, ["ZEST"], clock=FrozenClock(NOW1),
                               failures=[])
    assert rep2.skipped
    payload2 = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.estimate_snapshot_ingest.completed' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload2["skipped"] is True and payload2["rows_stored"] == 0


def test_universe_is_active_us_single_names(pg_session):
    s = pg_session
    _instrument(s, "ZST1")
    _instrument(s, "ZAD1", instrument_type="adr")
    _instrument(s, "ZETF", instrument_type="etf")     # ETF: no estimates
    _instrument(s, "ZAUS", market="AU")               # AU: outside ADR-0007 scope
    iid = _instrument(s, "ZOFF")
    s.execute(text("UPDATE market.instruments SET is_active = false "
                   "WHERE id = :iid"), {"iid": iid})
    syms = universe_symbols(s)
    assert "ZST1" in syms and "ZAD1" in syms
    assert not {"ZETF", "ZAUS", "ZOFF"} & set(syms)


def test_read_apis_latest_and_series(pg_session):
    s = pg_session
    _instrument(s, "ZEST")
    adapter = _FakeAdapter({"ZEST": DOC_DAY1})
    snapshot_estimates(s, adapter, ["ZEST"], now=NOW1, failures=[])
    adapter._docs["ZEST"] = DOC_DAY2
    snapshot_estimates(s, adapter, ["ZEST"], now=NOW2, failures=[])

    # latest_snapshot: the full consensus state as of a date
    latest = latest_snapshot(s, "ZEST", on=DAY2)
    assert latest is not None and len(latest) == 2
    assert [r.fiscal_period_end for r in latest] == [FPE1, FPE2]
    assert all(r.snapshot_date == DAY2 for r in latest)
    assert latest[0].eps_estimate_avg == Decimal("1.9500")
    d1 = latest_snapshot(s, "ZEST", on=DAY1)
    assert d1 is not None and all(r.snapshot_date == DAY1 for r in d1)
    assert d1[0].eps_estimate_avg == Decimal("1.9404")
    # no row at or before the date / unknown symbol -> None, never fabricated
    assert latest_snapshot(s, "ZEST", on=date(2026, 7, 16)) is None
    assert latest_snapshot(s, "ZNON", on=DAY2) is None

    # snapshot_series: the day-by-day PIT history for ONE fiscal period
    series = snapshot_series(s, "ZEST", fiscal_period_end=FPE1)
    assert [(r.snapshot_date, r.eps_estimate_avg) for r in series] == [
        (DAY1, Decimal("1.9404")), (DAY2, Decimal("1.9500"))]
    assert snapshot_series(s, "ZEST", fiscal_period_end=FPE1,
                           start=DAY2) == series[1:]
    assert snapshot_series(s, "ZEST", fiscal_period_end=FPE1,
                           end=DAY1) == series[:1]
    assert snapshot_series(s, "ZEST", fiscal_period_end=date(2030, 1, 1)) == ()
    assert snapshot_series(s, "ZNON", fiscal_period_end=FPE1) == ()
