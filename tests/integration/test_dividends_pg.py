"""Dividend ingest on atlas_test (fixtures only, no live calls): the
record_dividend natural-key idempotency, split-adjusted read-back, and the
fail-soft per-symbol backfill with its three honest states (ok / fetched-none
/ fetch-failed) and the coverage audit event.

No migration exists for dividends BY DESIGN: market.corporate_actions'
0001 CHECK constraint already permits action_type='dividend' and the table
carries dedicated amount/currency columns — these tests prove the storage
path against the real schema."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.dividends import backfill_dividends
from atlas.dcp.market_data.ingest import record_dividend, record_split
from atlas.dcp.market_data.models import Dividend, Split
from atlas.dcp.market_data.total_return import load_adjusted_dividends
from tests.conftest import requires_pg

pytestmark = requires_pg

FETCHED = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)


def _instrument(s, symbol: str) -> str:
    existing = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = :sym"),
        {"sym": symbol}).scalar()
    if existing is not None:
        return str(existing)
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'Broad', 'USD', FALSE) "
        "RETURNING id"), {"sym": symbol}).scalar())


def _div(symbol: str, ex: date, amount: str, currency: str | None = "USD") -> Dividend:
    return Dividend(symbol=symbol, ex_date=ex, amount=Decimal(amount),
                    currency=currency)


def test_record_dividend_idempotent_and_coexists_with_same_day_split(pg_session):
    s = pg_session
    iid = _instrument(s, "ZDIV0")
    d = _div("ZDIV0", date(2024, 3, 15), "1.25")
    record_dividend(s, iid, d, "EodhdAdapter")
    record_dividend(s, iid, d, "EodhdAdapter")   # natural-key arbiter: no dup
    # a split on the SAME date is a different action_type — both rows live
    record_split(s, iid, Split(symbol="ZDIV0", action_date=date(2024, 3, 15),
                               ratio=Decimal(2)), "EodhdAdapter")
    rows = s.execute(text(
        "SELECT action_type, ratio, amount, currency FROM "
        "market.corporate_actions WHERE instrument_id = :iid "
        "ORDER BY action_type"), {"iid": iid}).all()
    assert [r.action_type for r in rows] == ["dividend", "split"]
    div_row = rows[0]
    assert Decimal(div_row.amount) == Decimal("1.25")
    assert div_row.currency == "USD"
    assert div_row.ratio is None                 # ratio is split semantics only


def test_load_adjusted_dividends_applies_split_rule_on_read(pg_session):
    """Stored RAW, adjusted on read — the bars convention: a dividend before
    the split divides by the ratio; one after it is untouched."""
    s = pg_session
    iid = _instrument(s, "ZDIV1")
    record_split(s, iid, Split(symbol="ZDIV1", action_date=date(2020, 8, 31),
                               ratio=Decimal(4)), "EodhdAdapter")
    record_dividend(s, iid, _div("ZDIV1", date(2020, 5, 8), "0.82"), "EodhdAdapter")
    record_dividend(s, iid, _div("ZDIV1", date(2020, 11, 6), "0.205"), "EodhdAdapter")
    divs = load_adjusted_dividends(s, "ZDIV1")
    assert [(d.ex_date, d.amount) for d in divs] == [
        (date(2020, 5, 8), Decimal("0.205")),    # 0.82 / 4
        (date(2020, 11, 6), Decimal("0.205"))]


class _FakeAdapter:
    """ok symbol -> dividends; empty symbol -> []; bad symbol -> raises."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
        self.calls.append(symbol)
        if symbol == "ZDIVBAD":
            raise RuntimeError("vendor says 404 (delisted quirk)")
        if symbol == "ZDIVNONE":
            return []
        return [_div(symbol, date(2024, 3, 15), "1.00"),
                _div(symbol, date(2024, 6, 14), "1.10")]


def test_backfill_dividends_fail_soft_states_and_audit(pg_session):
    s = pg_session
    for sym in ("ZDIVOK", "ZDIVNONE", "ZDIVBAD"):
        _instrument(s, sym)
    audit = PostgresAuditLog(s, FrozenClock(FETCHED))
    adapter = _FakeAdapter()

    report = backfill_dividends(session=s, adapter=adapter, audit=audit,
                                symbols=["ZDIVOK", "ZDIVNONE", "ZDIVBAD"],
                                start=date(2024, 1, 1), end=date(2024, 12, 31))

    # one failure never aborts the batch — every symbol was attempted
    assert adapter.calls == ["ZDIVOK", "ZDIVNONE", "ZDIVBAD"]
    by = {r.symbol: r for r in report.symbols}
    assert by["ZDIVOK"].status == "ok" and by["ZDIVOK"].rows == 2
    assert by["ZDIVNONE"].status == "empty" and by["ZDIVNONE"].rows == 0
    assert by["ZDIVBAD"].status == "failed"
    assert "404" in by["ZDIVBAD"].error
    assert report.ok == 1 and report.empty == 1
    assert [f.symbol for f in report.failed] == ["ZDIVBAD"]

    stored = s.execute(text(
        "SELECT count(*) FROM market.corporate_actions ca "
        "JOIN market.instruments i ON i.id = ca.instrument_id "
        "WHERE i.symbol = 'ZDIVOK' AND ca.action_type = 'dividend'")).scalar()
    assert stored == 2

    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.dividends.backfill.completed' "
        "ORDER BY seq DESC LIMIT 1")).scalar()
    assert payload is not None
    assert payload["with_dividends"] == 1
    assert payload["fetched_none"] == 1           # distinguished from failure
    assert payload["fetch_failed"] == ["ZDIVBAD"]
    assert "404" in payload["fetch_failed_errors"]["ZDIVBAD"]
    assert payload["rows_fetched"] == 2
    assert payload["gates_written"] is False


def test_symbol_map_from_instruments_derives_vendor_codes(pg_session):
    """The instruments-derived fallback map (first real ingest: ALGM/PEGA had
    stored bars but no seeds row): known exchanges map to vendor codes, an
    unknown exchange fails loudly via vendor_symbol — never a silent .US."""
    from atlas.dcp.market_data.dividends import symbol_map_from_instruments
    s = pg_session
    s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES ('ZMAP1', 'NASDAQ', 'US', 'stock', 'ZMAP1', 'Broad', 'USD', "
        "FALSE) ON CONFLICT DO NOTHING"))
    assert symbol_map_from_instruments(s, ["ZMAP1"]) == {"ZMAP1": "ZMAP1.US"}
    _instrument(s, "ZMAP2")   # exchange XTEST: no EODHD suffix mapping
    with pytest.raises(ValueError, match="no EODHD suffix mapping"):
        symbol_map_from_instruments(s, ["ZMAP2"])


def test_backfill_dividends_unknown_symbol_refuses_loudly(pg_session):
    s = pg_session
    audit = PostgresAuditLog(s, FrozenClock(FETCHED))
    with pytest.raises(ValueError, match="unknown symbol"):
        backfill_dividends(session=s, adapter=_FakeAdapter(), audit=audit,
                           symbols=["ZNEVERSEEDED"],
                           start=date(2024, 1, 1), end=date(2024, 12, 31))


def test_backfill_dividends_rerun_is_idempotent(pg_session):
    s = pg_session
    _instrument(s, "ZDIVOK2")

    class _A(_FakeAdapter):
        def fetch_dividends(self, symbol: str, start: date, end: date) -> list[Dividend]:
            return [_div(symbol, date(2024, 3, 15), "1.00")]

    audit = PostgresAuditLog(s, FrozenClock(FETCHED))
    for _ in range(2):
        backfill_dividends(session=s, adapter=_A(), audit=audit,
                           symbols=["ZDIVOK2"],
                           start=date(2024, 1, 1), end=date(2024, 12, 31))
    stored = s.execute(text(
        "SELECT count(*) FROM market.corporate_actions ca "
        "JOIN market.instruments i ON i.id = ca.instrument_id "
        "WHERE i.symbol = 'ZDIVOK2' AND ca.action_type = 'dividend'")).scalar()
    assert stored == 1
