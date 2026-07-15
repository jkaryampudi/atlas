"""Earnings::History parser + ingest: completed-quarters-only, append-only
immutability, fail-soft per instrument, and the audit event with counts."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.earnings_history import (
    ingest_earnings_history,
    ingest_with_audit,
    parse_earnings_history,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))


def _doc(history: dict, currency: str = "USD") -> dict:
    return {"General": {"CurrencyCode": currency}, "Earnings": {"History": history}}


# A rich History: two completed quarters, one still-forward (epsActual null),
# one estimate-less legacy row, one out-of-vocab timing flag, one malformed key.
FULL_HISTORY = {
    "2023-03-31": {"reportDate": "2023-05-01", "date": "2023-03-31",
                   "beforeAfterMarket": "AfterMarket", "currency": "USD",
                   "epsActual": 1.10, "epsEstimate": 1.00, "epsDifference": 0.10,
                   "surprisePercent": 10.0},
    "2023-06-30": {"reportDate": "2023-08-01", "date": "2023-06-30",
                   "beforeAfterMarket": "weird-o-clock", "currency": "USD",
                   "epsActual": 1.20, "epsEstimate": 1.05, "epsDifference": 0.15,
                   "surprisePercent": 14.2857},
    "2023-09-30": {"reportDate": "2023-11-01", "date": "2023-09-30",
                   "beforeAfterMarket": "BeforeMarket", "currency": "USD",
                   "epsActual": None, "epsEstimate": 1.10},   # not yet reported
    "2001-03-31": {"reportDate": "2001-05-01", "date": "2001-03-31",
                   "epsActual": 0.50, "epsEstimate": None},   # no consensus leg
    "not-a-date": {"reportDate": "2023-05-01", "epsActual": 1.0, "epsEstimate": 1.0},
}


class _FakeAdapter:
    """Minimal fundamentals adapter: serves docs for known symbols, an empty
    History for 'EMPTY', and raises for 'BOOM' (vendor failure)."""
    def __init__(self, docs: dict[str, dict]) -> None:
        self._docs = docs

    def fetch_fundamentals(self, symbol: str) -> dict:
        if symbol == "BOOM":
            raise RuntimeError("vendor 500")
        return self._docs[symbol]


def test_parse_completed_quarters_only():
    rows = parse_earnings_history(_doc(FULL_HISTORY), "ZP")
    # only the two completed rows survive; sorted by fiscal_period_end
    assert [r.fiscal_period_end.isoformat() for r in rows] == \
        ["2023-03-31", "2023-06-30"]
    r0, r1 = rows
    assert r0.before_after_market == "AfterMarket"
    assert r1.before_after_market is None          # out-of-vocab flag dropped
    assert float(r0.eps_actual) == 1.10 and float(r0.eps_estimate) == 1.00
    assert float(r0.surprise_pct) == 10.0
    assert r0.currency == "USD"


def test_parse_empty_history_is_valid():
    assert parse_earnings_history(_doc({}), "ZE") == []
    assert parse_earnings_history({"Earnings": {}}, "ZE") == []
    assert parse_earnings_history({}, "ZE") == []


def _instrument(s, symbol: str) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'USD') RETURNING id"),
        {"sym": symbol}).scalar())


def test_ingest_fail_soft_and_append_only(pg_session):
    s = pg_session
    for sym in ("ZOK", "EMPTY", "BOOM"):        # ZNONE deliberately not seeded
        _instrument(s, sym)
    adapter = _FakeAdapter({"ZOK": _doc(FULL_HISTORY), "EMPTY": _doc({})})

    failures: list[str] = []
    rep = ingest_earnings_history(
        s, adapter, ["ZOK", "EMPTY", "BOOM", "ZNONE"],
        now=CLOCK.now(), failures=failures)

    assert rep.fetched == ("ZOK",)
    assert rep.empty == ("EMPTY",)
    assert set(rep.failed) == {"BOOM", "ZNONE"}   # vendor failure + missing row
    assert rep.stored == 2                        # two completed quarters
    assert len(failures) == 2

    stored = s.execute(text(
        "SELECT count(*) FROM market.earnings_surprises es "
        "JOIN market.instruments i ON i.id = es.instrument_id "
        "WHERE i.symbol = 'ZOK'")).scalar()
    assert stored == 2

    # append-only: a re-run stores ZERO new rows (idempotent on the natural key)
    rep2 = ingest_earnings_history(s, adapter, ["ZOK"], now=CLOCK.now(),
                                   failures=[])
    assert rep2.stored == 0


def test_ingest_with_audit_emits_counts(pg_session):
    s = pg_session
    _instrument(s, "ZOK")
    adapter = _FakeAdapter({"ZOK": _doc(FULL_HISTORY)})
    failures: list[str] = []
    ingest_with_audit(s, adapter, ["ZOK", "BOOM"], clock=CLOCK, failures=failures)

    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.earnings_history_ingest.completed' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload is not None
    assert payload["rows_stored"] == 2
    assert payload["fetched"] == ["ZOK"]
    assert payload["failed"] == ["BOOM"]
    assert payload["coverage"]["rows"] == 2
    assert payload["symbols"] == 2
