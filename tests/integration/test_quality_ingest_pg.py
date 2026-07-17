"""Quarterly-fundamentals parser + ingest: anchorable-quarters-only (the
degenerate filing_date == period end vendor defect dropped fail-closed and
COUNTED), the IS/BS merge by fiscal-period-end with max-filing knowability,
append-only immutability, fail-soft per instrument, and the audit event with
honesty counts."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.quarterly_fundamentals import (
    ingest_quarterly_fundamentals,
    ingest_with_audit,
    parse_quarterly_fundamentals,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 17, 12, 0, tzinfo=UTC))


def _doc(income: dict, balance: dict) -> dict:
    return {"General": {"CurrencyCode": "USD"},
            "Financials": {"Income_Statement": {"quarterly": income},
                           "Balance_Sheet": {"quarterly": balance}}}


# A rich document exercising every parse rule. Values are decimal STRINGS —
# the vendor's real wire shape ('14919000000.00', probed live 2026-07).
INCOME = {
    # complete quarter, filing 31 days after period end
    "2023-03-31": {"date": "2023-03-31", "filing_date": "2023-05-01",
                   "currency_symbol": "USD", "grossProfit": "10.00",
                   "totalRevenue": "20.00"},
    # DEGENERATE: filing_date == period end (the probed AVGO defect) -> dropped
    "2023-06-30": {"date": "2023-06-30", "filing_date": "2023-06-30",
                   "currency_symbol": "USD", "grossProfit": "11.00",
                   "totalRevenue": "21.00"},
    # anchorable but grossProfit missing -> stored with NULL gp (missing is
    # missing; totalRevenue alone is a valid partial fact)
    "2023-09-30": {"date": "2023-09-30", "filing_date": "2023-11-01",
                   "currency_symbol": "USD", "grossProfit": None,
                   "totalRevenue": "22.00"},
    # no filing_date at all -> unanchorable, dropped
    "2023-12-31": {"date": "2023-12-31", "currency_symbol": "USD",
                   "grossProfit": "12.00", "totalRevenue": "23.00"},
    # anchorable, IS filing EARLIER than the BS filing (merge takes the max)
    "2024-03-31": {"date": "2024-03-31", "filing_date": "2024-05-01",
                   "currency_symbol": "USD", "grossProfit": "13.00",
                   "totalRevenue": "24.00"},
    # anchorable but EVERY metric absent -> metricless, dropped
    "2024-06-30": {"date": "2024-06-30", "filing_date": "2024-08-01",
                   "currency_symbol": "USD"},
    # malformed key -> skipped silently (vendor noise)
    "not-a-date": {"filing_date": "2024-08-01", "grossProfit": "1.00"},
}
BALANCE = {
    "2023-03-31": {"date": "2023-03-31", "filing_date": "2023-05-01",
                   "currency_symbol": "USD", "totalAssets": "100.00"},
    "2023-06-30": {"date": "2023-06-30", "filing_date": "2023-06-30",
                   "currency_symbol": "USD", "totalAssets": "101.00"},
    # 2023-09-30 has NO balance row: total_assets stays NULL on the merged row
    "2024-03-31": {"date": "2024-03-31", "filing_date": "2024-05-03",
                   "currency_symbol": "USD", "totalAssets": "104.00"},
    # BS-only quarter (no income row): stored with NULL gp/revenue
    "2024-09-30": {"date": "2024-09-30", "filing_date": "2024-11-01",
                   "currency_symbol": "USD", "totalAssets": "106.00"},
}


def test_parse_anchorable_quarters_only_with_honest_counts():
    parsed = parse_quarterly_fundamentals(_doc(INCOME, BALANCE), "ZQ")
    assert [r.fiscal_period_end.isoformat() for r in parsed.rows] == \
        ["2023-03-31", "2023-09-30", "2024-03-31", "2024-09-30"]
    assert parsed.degenerate_filing == 1     # 2023-06-30 (the probed defect)
    assert parsed.unanchorable == 1          # 2023-12-31 (no filing_date)
    assert parsed.metricless == 1            # 2024-06-30

    r0, r1, r2, r3 = parsed.rows
    # complete merged quarter
    assert r0.filing_date == date(2023, 5, 1)
    assert r0.gross_profit == Decimal("10.00")
    assert r0.total_revenue == Decimal("20.00")
    assert r0.total_assets == Decimal("100.00")
    assert r0.currency == "USD"
    # missing grossProfit stays missing (never derived); no BS row -> NULL assets
    assert r1.gross_profit is None and r1.total_revenue == Decimal("22.00")
    assert r1.total_assets is None
    # divergent statement filings: knowability = the LATER one (max)
    assert r2.filing_date == date(2024, 5, 3)
    # balance-only quarter
    assert r3.gross_profit is None and r3.total_assets == Decimal("106.00")


def test_parse_empty_documents_are_valid():
    assert parse_quarterly_fundamentals(_doc({}, {}), "ZE").rows == ()
    assert parse_quarterly_fundamentals({"Financials": {}}, "ZE").rows == ()
    assert parse_quarterly_fundamentals({}, "ZE").rows == ()   # ETFs have none


class _FakeAdapter:
    """Minimal fundamentals adapter: serves docs for known symbols, an empty
    document for 'EMPTY', and raises for 'BOOM' (vendor failure)."""
    def __init__(self, docs: dict[str, dict]) -> None:
        self._docs = docs

    def fetch_fundamentals(self, symbol: str) -> dict:
        if symbol == "BOOM":
            raise RuntimeError("vendor 500")
        return self._docs[symbol]


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
    adapter = _FakeAdapter({"ZOK": _doc(INCOME, BALANCE), "EMPTY": _doc({}, {})})

    failures: list[str] = []
    rep = ingest_quarterly_fundamentals(
        s, adapter, ["ZOK", "EMPTY", "BOOM", "ZNONE"],
        now=CLOCK.now(), failures=failures)

    assert rep.fetched == ("ZOK",)
    assert rep.empty == ("EMPTY",)
    assert set(rep.failed) == {"BOOM", "ZNONE"}   # vendor failure + missing row
    assert rep.stored == 4                        # four anchorable quarters
    assert rep.degenerate_filing == 1
    assert rep.unanchorable == 1
    assert rep.metricless == 1
    assert len(failures) == 2

    stored = s.execute(text(
        "SELECT count(*) FROM market.quarterly_fundamentals qf "
        "JOIN market.instruments i ON i.id = qf.instrument_id "
        "WHERE i.symbol = 'ZOK'")).scalar()
    assert stored == 4

    # append-only: a re-run stores ZERO new rows (idempotent on the natural key)
    rep2 = ingest_quarterly_fundamentals(s, adapter, ["ZOK"], now=CLOCK.now(),
                                         failures=[])
    assert rep2.stored == 0


def test_ingest_with_audit_emits_counts(pg_session):
    s = pg_session
    _instrument(s, "ZOK")
    adapter = _FakeAdapter({"ZOK": _doc(INCOME, BALANCE)})
    failures: list[str] = []
    ingest_with_audit(s, adapter, ["ZOK", "BOOM"], clock=CLOCK, failures=failures)

    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.quarterly_fundamentals_ingest.completed' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload is not None
    assert payload["rows_stored"] == 4
    assert payload["fetched"] == ["ZOK"]
    assert payload["failed"] == ["BOOM"]
    assert payload["dropped_degenerate_filing"] == 1
    assert payload["dropped_unanchorable"] == 1
    assert payload["dropped_metricless"] == 1
    assert payload["coverage"]["rows"] == 4
    assert payload["symbols"] == 2
