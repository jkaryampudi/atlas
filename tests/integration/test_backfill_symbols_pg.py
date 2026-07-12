"""backfill_symbols (the additive --symbols validation mode): bars + splits
for EXACTLY the named symbols regardless of is_active, through the SAME
chunking/upsert machinery — and NO quality gates, ever (gate coverage is a
tradable-universe contract; see the backfill module docstring). FX must not
even be touched. All fail-closed edges pinned: unknown symbol refuses, a
requested symbol the vendor returns nothing for is a counted failure."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.backfill import backfill_symbols, chunk_windows
from atlas.dcp.market_data.models import Bar, Split
from tests.conftest import requires_pg

pytestmark = requires_pg

START, END = date(2010, 1, 1), date(2020, 12, 31)  # > CHUNK_DAYS: multi-chunk


def _instrument(s, symbol: str, *, active: bool) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES (:sym, 'XTEST', 'US', 'etf', :sym, 'Broad', 'USD', :act) "
        "RETURNING id"), {"sym": symbol, "act": active}).scalar())


class _StubAdapter:
    """Five bars + one split per known symbol; records every bar-fetch window
    so chunk reuse is pinned; explodes if FX is touched."""

    def __init__(self, known: set[str]) -> None:
        self.known = known
        self.bar_windows: list[tuple[str, date, date]] = []

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        self.bar_windows.append((symbol, start, end))
        if symbol not in self.known:
            return []
        return [Bar(symbol=symbol, bar_date=START + timedelta(days=i),
                    open=Decimal(100), high=Decimal(101), low=Decimal(99),
                    close=Decimal(100), volume=1000)
                for i in range(5) if start <= START + timedelta(days=i) <= end]

    def fetch_splits(self, symbol: str, start: date, end: date) -> list[Split]:
        if symbol not in self.known:
            return []
        return [Split(symbol=symbol, action_date=date(2015, 6, 1),
                      ratio=Decimal(2))]

    def fetch_fx(self, base: str, quote: str, on: date) -> Decimal | None:
        raise AssertionError("FX must not be touched in symbols mode")

    def fetch_fx_series(self, base: str, quote: str, start: date,
                        end: date) -> dict[date, Decimal]:
        raise AssertionError("FX must not be touched in symbols mode")


def _counts(s) -> tuple[int, int, int]:
    return s.execute(text(
        "SELECT (SELECT count(*) FROM market.price_bars_daily),"
        "(SELECT count(*) FROM market.corporate_actions),"
        "(SELECT count(*) FROM market.data_quality_gates)")).one()


def test_symbols_mode_backfills_regardless_of_is_active_and_writes_no_gates(
        clean_audit):
    s = clean_audit
    _instrument(s, "ZBA", active=True)
    _instrument(s, "ZBI", active=False)
    gates_before = _counts(s)[2]
    adapter = _StubAdapter(known={"ZBA", "ZBI"})
    audit = PostgresAuditLog(s, FrozenClock(datetime(2021, 1, 4, 22, tzinfo=UTC)))

    report = backfill_symbols(session=s, adapter=adapter, audit=audit,
                              symbols=["ZBI", "ZBA"], start=START, end=END)

    assert not report.failed
    by_sym = {r.symbol: r for r in report.symbols}
    assert set(by_sym) == {"ZBA", "ZBI"}
    for r in report.symbols:
        assert r.bars == 5 and r.splits == 1 and r.inception == START
    stored = s.execute(text(
        "SELECT i.symbol, count(*) FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol IN ('ZBA','ZBI') GROUP BY i.symbol")).all()
    assert {r.symbol: r.count for r in stored} == {"ZBA": 5, "ZBI": 5}
    # THE contract: no gate rows for a validation (inactive-included) run
    assert _counts(s)[2] == gates_before
    # chunk machinery reused verbatim: per symbol, exactly chunk_windows()
    expected = [(sym, lo, hi) for sym in ("ZBI", "ZBA")
                for lo, hi in chunk_windows(START, END)]
    assert adapter.bar_windows == expected
    assert len(chunk_windows(START, END)) > 1     # window really is multi-chunk


def test_symbols_mode_emits_audit_event_with_gate_rationale(clean_audit):
    s = clean_audit
    _instrument(s, "ZBI", active=False)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2021, 1, 4, 22, tzinfo=UTC)))
    backfill_symbols(session=s, adapter=_StubAdapter(known={"ZBI"}), audit=audit,
                     symbols=["ZBI"], start=START, end=END)
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'market.backfill.symbols.completed'")).scalar()
    assert payload["gates_written"] is False
    assert "tradable-universe contract" in payload["gates_rationale"]
    assert payload["symbols"]["ZBI"]["bars"] == 5


def test_symbols_mode_is_idempotent(clean_audit):
    s = clean_audit
    _instrument(s, "ZBI", active=False)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2021, 1, 4, 22, tzinfo=UTC)))
    args = dict(session=s, adapter=_StubAdapter(known={"ZBI"}), audit=audit,
                symbols=["ZBI"], start=START, end=END)
    backfill_symbols(**args)
    before = _counts(s)
    backfill_symbols(**args)
    assert _counts(s) == before


def test_symbols_mode_refuses_unknown_symbol(clean_audit):
    s = clean_audit
    audit = PostgresAuditLog(s, FrozenClock(datetime(2021, 1, 4, 22, tzinfo=UTC)))
    with pytest.raises(ValueError, match=r"unknown symbol\(s\) \['ZGHOST'\]"):
        backfill_symbols(session=s, adapter=_StubAdapter(known=set()),
                         audit=audit, symbols=["ZGHOST"], start=START, end=END)


def test_symbols_mode_counts_a_barless_symbol_as_failure(clean_audit):
    s = clean_audit
    _instrument(s, "ZBEMPTY", active=False)
    audit = PostgresAuditLog(s, FrozenClock(datetime(2021, 1, 4, 22, tzinfo=UTC)))
    report = backfill_symbols(session=s, adapter=_StubAdapter(known=set()),
                              audit=audit, symbols=["ZBEMPTY"],
                              start=START, end=END)
    assert report.failed
    assert report.symbols[0].inception is None
