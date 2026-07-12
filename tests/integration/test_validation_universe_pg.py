"""Validation-only instruments (is_active = FALSE) must be INVISIBLE to every
tradable-universe surface — quality inception, gate coverage, the scanner, the
desk — and to the default xsmom panel; they are visible ONLY to the explicit
symbols mode. Each pin is behavioural and bidirectional where it matters: the
inactive instrument carries clean vendor bars that WOULD qualify it on every
surface, so absence proves the is_active filter and nothing else; the gate
test also flips the instrument active to show the gate would catch it.

Everything runs inside the test transaction (rolled back at teardown), against
atlas_test only (conftest guard)."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from atlas.agents.desk import desk_symbols
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.xsmom_run import load_universe_panel
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.ingest import ingest_day
from atlas.dcp.market_data.models import Bar, GateStatus
from atlas.dcp.market_data.quality import inception_map
from atlas.dcp.market_data.validation_universe import (
    VALIDATION_SEEDS,
    seed_validation_instruments,
)
from atlas.dcp.scanner.v1 import scan
from tests.conftest import requires_pg

pytestmark = requires_pg

T = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)  # XNYS 2026-07-15 closed 20:00 UTC
SESSIONS = trading_days_between("US", date(2026, 4, 1), date(2026, 7, 15))[-60:]


def _instrument(s, symbol: str, *, active: bool) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES (:sym, 'XTEST', 'US', 'etf', :sym, 'Broad', 'USD', :act) "
        "RETURNING id"), {"sym": symbol, "act": active}).scalar())


def _bars(s, iid: str, dates: list[date]) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, 100, 100, 100, 100, 1000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d} for d in dates])


def _clean_trading(s) -> None:
    """Committed leftovers from aborted runs must never shape a scan
    (mirrors test_scanner_pg._clean)."""
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots",
              "trading.reconciliations"):
        s.execute(text(f"DELETE FROM {t}"))


def _seed_pair(s) -> tuple[str, str]:
    """One ACTIVE (ZVA) and one INACTIVE (ZVI) instrument with IDENTICAL clean
    vendor bars; everything else deactivated in-txn so each surface's output
    is fully determined by this pair."""
    s.execute(text("UPDATE market.instruments SET is_active = false"))
    ida = _instrument(s, "ZVA", active=True)
    idi = _instrument(s, "ZVI", active=False)
    _bars(s, ida, SESSIONS)
    _bars(s, idi, SESSIONS)
    return ida, idi


def test_seed_validation_instruments_inserts_inactive_and_is_idempotent(pg_session):
    s = pg_session
    s.execute(text("DELETE FROM market.instruments "
                   "WHERE symbol LIKE 'XL_' AND exchange = 'NYSEARCA'"))
    first = seed_validation_instruments(s, VALIDATION_SEEDS)
    assert len(first.inserted) == 9 and not first.already_present
    rows = s.execute(text(
        "SELECT symbol, is_active FROM market.instruments "
        "WHERE exchange = 'NYSEARCA' AND symbol LIKE 'XL_' ORDER BY symbol")).all()
    assert [r.symbol for r in rows] == ["XLB", "XLE", "XLF", "XLI", "XLK",
                                        "XLP", "XLU", "XLV", "XLY"]
    assert all(r.is_active is False for r in rows)
    again = seed_validation_instruments(s, VALIDATION_SEEDS)
    assert not again.inserted and len(again.already_present) == 9


def test_seed_refuses_collision_with_an_active_instrument(pg_session, tmp_path):
    s = pg_session
    _instrument(s, "ZCOLL", active=True)
    csv_path = tmp_path / "validation.csv"
    csv_path.write_text(
        "symbol,exchange,market,instrument_type,name,sector_gics,currency,"
        "economic_exposure\n"
        "ZCOLL,NYSEARCA,US,etf,Collision Fund,Broad,USD,US\n")
    with pytest.raises(ValueError, match="collides with an ACTIVE instrument"):
        seed_validation_instruments(s, csv_path)


def test_inactive_invisible_to_quality_inception(pg_session):
    s = pg_session
    _seed_pair(s)
    for market in (None, "US"):
        m = inception_map(s, market)
        assert m.get("ZVA") == SESSIONS[0]
        assert "ZVI" not in m


class _RecordingAdapter:
    """Serves clean bars for ZVA only and records every symbol requested —
    an inactive instrument must never even be FETCHED."""

    def __init__(self) -> None:
        self.requested: set[str] = set()

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        self.requested.add(symbol)
        if symbol != "ZVA":
            return []
        return [Bar(symbol=symbol, bar_date=d, open=100, high=100, low=100,
                    close=100, volume=1000)
                for d in SESSIONS if start <= d <= end]

    def fetch_splits(self, symbol: str, start: date, end: date) -> list:
        return []


def test_inactive_invisible_to_gate_coverage_and_would_red_if_active(pg_session):
    s = pg_session
    _seed_pair(s)
    audit = PostgresAuditLog(s, FrozenClock(T))
    day = SESSIONS[-1]

    adapter = _RecordingAdapter()
    status = ingest_day(session=s, adapter=adapter, audit=audit,
                        market="US", day=day)
    assert status is GateStatus.GREEN          # ZVI's absence is not a gap
    assert "ZVI" not in adapter.requested      # never even fetched
    reasons = s.execute(text(
        "SELECT reasons FROM market.data_quality_gates "
        "WHERE market = 'US' AND gate_date = :d"), {"d": day}).scalar()
    assert "ZVI" not in str(reasons)

    # bidirectional: the SAME series with is_active=true must red the gate
    # (the adapter serves ZVA only), proving invisibility comes from the
    # filter, not from the gate being blind.
    s.execute(text("UPDATE market.instruments SET is_active = true "
                   "WHERE symbol = 'ZVI'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id = "
                   "(SELECT id FROM market.instruments WHERE symbol = 'ZVI')"))
    status = ingest_day(session=s, adapter=adapter, audit=audit,
                        market="US", day=day)
    assert status is GateStatus.RED
    assert "ZVI" in adapter.requested


def test_inactive_invisible_to_scanner(pg_session):
    s = pg_session
    _clean_trading(s)
    _seed_pair(s)
    report = scan(s, FrozenClock(T))
    assert report.scanned == 1                 # only the active instrument
    symbols_seen = ({e.symbol for e in report.shortlist}
                    | {sym for sym, _ in report.ineligible})
    assert "ZVA" in symbols_seen
    assert "ZVI" not in symbols_seen


def test_inactive_invisible_to_desk(pg_session):
    s = pg_session
    _seed_pair(s)
    symbols = desk_symbols(s)                  # both have 60 vendor bars >= 51
    assert symbols == ["ZVA"]


def test_inactive_invisible_to_default_xsmom_panel_visible_to_symbols_mode(pg_session):
    s = pg_session
    _seed_pair(s)
    default = load_universe_panel(s)
    assert default.included == ["ZVA"]
    assert "ZVI" not in {e.symbol for e in default.excluded}

    explicit = load_universe_panel(s, symbols=["ZVI"])
    assert explicit.included == ["ZVI"]

    with pytest.raises(RuntimeError, match="no vendor bars"):
        load_universe_panel(s, symbols=["ZVI", "ZNOPE"])
