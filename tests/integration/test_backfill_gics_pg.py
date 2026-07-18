"""GICS backfill (atlas/tools/backfill_gics.py, ADR-0016 decision 2) against
the isolated test DB.

Matrix: GicSector written verbatim; Sector fallback translated through the
closed mapping; out-of-vocabulary and vendor-failure names left missing and
reported (they then fail closed out of activation); vendor-delisted members
skipped WITHOUT a fetch; non-missing sectors never overwritten (idempotent);
former members are not candidates; dry-run writes nothing; apply emits ONE
audit event with the full counts.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Mapping

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.tools.backfill_gics import GicsBackfillReport, backfill_gics
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 18, 10, 0, tzinfo=UTC))
FETCHED = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


def _clean(s) -> None:
    s.execute(text("DELETE FROM validation.index_membership"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZUB%'"))


def _member(s, ticker: str, *, active_now: bool = True,
            delisted: bool = False) -> None:
    s.execute(text(
        "INSERT INTO validation.index_membership (index_code, ticker, name, "
        "start_date, end_date, is_active_now, is_delisted, fetched_at) "
        "VALUES ('GSPC.INDX', :t, :t, '2020-01-02', NULL, :a, :d, :f)"),
        {"t": ticker, "a": active_now, "d": delisted, "f": FETCHED})


def _instrument(s, symbol: str, *, sector: str | None = "",
                exchange: str = "US") -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES (:sym, :ex, 'US', 'stock', :sym, :sec, 'USD', FALSE) "
        "RETURNING id"), {"sym": symbol, "ex": exchange, "sec": sector}).scalar())


def _sector_of(s, symbol: str) -> str | None:
    return s.execute(text(
        "SELECT sector_gics FROM market.instruments WHERE symbol = :s"),
        {"s": symbol}).scalar()


class StubFetch:
    """Records the vendor codes fetched; raises where told to."""

    def __init__(self, payloads: dict[str, Mapping[str, object]],
                 boom: set[str] = frozenset()) -> None:
        self.payloads = payloads
        self.boom = set(boom)
        self.calls: list[str] = []

    def __call__(self, code: str) -> Mapping[str, object]:
        self.calls.append(code)
        if code in self.boom:
            raise LookupError(f"EODHD has no fundamentals for {code!r}")
        return self.payloads[code]


@pytest.fixture
def seeded(pg_session):
    s = pg_session
    _clean(s)
    # ZUBA: '' sector, GicSector present            -> updated verbatim
    # ZUBB: NULL sector, Sector-only fallback       -> updated via mapping
    # ZUBC: '' sector, both fields out-of-vocab     -> unresolved, left ''
    # ZUBD: '' sector, vendor 404                   -> unresolved, left ''
    # ZUBE: '' sector, vendor-delisted              -> skipped, never fetched
    # ZUBF: sector already present                  -> never fetched, kept
    # ZUBG: '' sector, NOT a member                 -> untouched entirely
    # ZUBH: '' sector, FORMER member                -> not a candidate
    for sym, sector in [("ZUBA", ""), ("ZUBB", None), ("ZUBC", ""),
                        ("ZUBD", ""), ("ZUBE", ""), ("ZUBF", "Energy"),
                        ("ZUBG", ""), ("ZUBH", "")]:
        _instrument(s, sym, sector=sector)
    for sym in ("ZUBA", "ZUBB", "ZUBC", "ZUBD", "ZUBF"):
        _member(s, sym)
    _member(s, "ZUBE", delisted=True)
    _member(s, "ZUBH", active_now=False)
    fetch = StubFetch({
        "ZUBA.US": {"General": {"GicSector": "Health Care",
                                "Sector": "Healthcare"}},
        "ZUBB.US": {"General": {"Sector": "Technology"}},
        "ZUBC.US": {"General": {"GicSector": "Junk", "Sector": "Widgets"}},
        # ZUBF payload disagrees with the stored sector on purpose: it must
        # never even be fetched, let alone overwrite the reviewed value.
        "ZUBF.US": {"General": {"GicSector": "Utilities"}},
    }, boom={"ZUBD.US"})
    return s, fetch


def _events(s, event_type: str) -> int:
    return s.execute(text(
        "SELECT count(*) FROM audit.decision_events WHERE event_type = :t"),
        {"t": event_type}).scalar()


def test_dry_run_reports_but_writes_nothing(seeded):
    s, fetch = seeded
    before = _events(s, "market.instruments.gics_backfilled")
    report = backfill_gics(s, fetch, apply=False)
    assert report.updated == (("ZUBA", "Health Care"),
                              ("ZUBB", "Information Technology"))
    assert [sym for sym, _ in report.unresolved] == ["ZUBC", "ZUBD"]
    assert report.skipped_delisted == ("ZUBE",)
    assert report.already_have_sector == 1          # ZUBF
    # candidates only: ZUBE (delisted) and ZUBF (has sector) never fetched
    assert sorted(fetch.calls) == ["ZUBA.US", "ZUBB.US", "ZUBC.US", "ZUBD.US"]
    # nothing written: sectors unchanged, no audit event
    assert _sector_of(s, "ZUBA") == ""
    assert _sector_of(s, "ZUBB") is None
    assert _events(s, "market.instruments.gics_backfilled") == before


def test_apply_updates_only_resolvable_and_audits_once(seeded):
    s, fetch = seeded
    audit = PostgresAuditLog(s, CLOCK)
    before = _events(s, "market.instruments.gics_backfilled")
    report = backfill_gics(s, fetch, apply=True, audit=audit)
    assert report.updated == (("ZUBA", "Health Care"),
                              ("ZUBB", "Information Technology"))
    assert _sector_of(s, "ZUBA") == "Health Care"
    assert _sector_of(s, "ZUBB") == "Information Technology"
    # unresolvable stays missing (then fails closed out of activation)
    assert _sector_of(s, "ZUBC") == ""
    assert _sector_of(s, "ZUBD") == ""
    # delisted skipped, existing value untouched, non-member untouched
    assert _sector_of(s, "ZUBE") == ""
    assert _sector_of(s, "ZUBF") == "Energy"
    assert _sector_of(s, "ZUBG") == ""
    assert _sector_of(s, "ZUBH") == ""
    # exactly ONE audit event, with the full counts
    assert _events(s, "market.instruments.gics_backfilled") == before + 1
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.instruments.gics_backfilled' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload["updated"] == {"ZUBA": "Health Care",
                                  "ZUBB": "Information Technology"}
    assert sorted(payload["unresolved"]) == ["ZUBC", "ZUBD"]
    assert payload["skipped_delisted"] == ["ZUBE"]
    assert payload["already_have_sector"] == 1
    assert payload["dry_run"] is False


def test_apply_requires_an_audit_log(seeded):
    s, fetch = seeded
    with pytest.raises(ValueError, match="audit"):
        backfill_gics(s, fetch, apply=True, audit=None)
    assert _sector_of(s, "ZUBA") == ""              # refused before any write


def test_second_run_is_idempotent(seeded):
    s, fetch = seeded
    audit = PostgresAuditLog(s, CLOCK)
    backfill_gics(s, fetch, apply=True, audit=audit)
    fetch.calls.clear()
    report = backfill_gics(s, fetch, apply=True, audit=audit)
    # ZUBA/ZUBB now carry sectors -> skipped as non-missing, never re-fetched
    assert report.updated == ()
    assert report.already_have_sector == 3          # ZUBA, ZUBB, ZUBF
    assert sorted(fetch.calls) == ["ZUBC.US", "ZUBD.US"]   # still missing
    assert _sector_of(s, "ZUBA") == "Health Care"


def test_report_is_frozen_and_ordered():
    r = GicsBackfillReport(updated=(("A", "Energy"),), unresolved=(),
                           skipped_delisted=(), already_have_sector=0)
    with pytest.raises(AttributeError):
        r.updated = ()  # type: ignore[misc]
