"""ANALYZE-ANY-TICKER orchestration (atlas/ops/analyze.py), fully stubbed —
no vendor, no model. Pins:

- unknown symbol => inserted as an ANALYSIS-ONLY instrument (is_active=FALSE,
  US-only v1) with bars + splits + one fundamentals snapshot upserted;
- phase transitions fetching -> analyzing -> done, observable while running;
- the non-blocking lock answers a second request 'busy', never runs twice;
- the desk receives exactly [symbol] and the verbatim source;
- failure paths are honest (vendor down / zero bars => phase failed with the
  reason) and always release the lock;
- known fresh symbol => zero vendor calls (staleness conventions respected).

The worker thread reaches the DB through the app engine (session_scope), so
these tests point ATLAS_DATABASE_URL at the isolated test DB and reset the
cached engine — the same pattern every API test uses.
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

import atlas.ops.analyze as analyze
from atlas.agents.desk import DeskMemo, DeskReport
from atlas.dcp.market_data.calendars import last_completed_session, trading_days_between
from atlas.dcp.market_data.models import Bar
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg


def _wait(predicate, timeout=15.0, msg="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {msg}; status={analyze.analysis_status()}")


def _wait_finished():
    _wait(lambda: analyze.analysis_status()["phase"] in ("done", "failed")
          and not analyze.analysis_status()["running"], msg="analysis to finish")


@pytest.fixture
def app_db(monkeypatch, pg_session):
    """Point the app engine (used by the worker thread) at the test DB."""
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    yield pg_session
    _wait(lambda: not analyze._analysis_lock.locked(), msg="lock release")
    analyze._status.update(phase="idle", symbol=None, source=None, started_at=None,
                           finished_at=None, detail=None, result=None)
    reset_app_engine()


@pytest.fixture
def cleanup_symbols(pg_session):
    """Remove instruments this file creates — a lingering row would leak into
    other tests' symbol-keyed queries."""
    symbols: list[str] = []
    yield symbols
    s = pg_session
    for sym in symbols:
        for tbl, col in (("market.price_bars_daily", "instrument_id"),
                         ("market.fundamentals", "instrument_id"),
                         ("market.corporate_actions", "instrument_id")):
            s.execute(text(f"DELETE FROM {tbl} WHERE {col} IN "
                           "(SELECT id FROM market.instruments WHERE symbol = :s)"),
                      {"s": sym})
        s.execute(text("DELETE FROM market.instruments WHERE symbol = :s"), {"s": sym})
    s.commit()


def _sessions(n: int) -> list:
    """The last n completed US sessions, ascending."""
    if n <= 0:
        return []
    end = last_completed_session("US", datetime.now(UTC))
    return trading_days_between("US", end - timedelta(days=n * 2 + 30), end)[-n:]


class StubAdapter:
    """Deterministic vendor: n_bars sessions of dailies, one fundamentals doc.
    Optional events let a test hold the worker inside a phase."""

    def __init__(self, n_bars: int = 60, gate: threading.Event | None = None):
        self.n_bars = n_bars
        self.gate = gate
        self.calls: list[str] = []

    def fetch_splits(self, symbol, start, end):
        self.calls.append("splits")
        if self.gate is not None:
            assert self.gate.wait(timeout=15)
        return []

    def fetch_bars(self, symbol, start, end):
        self.calls.append("bars")
        ten = Decimal("10")
        return [Bar(symbol=symbol, bar_date=d, open=ten, high=Decimal("11"),
                    low=Decimal("9"), close=Decimal("10.5"), volume=1000)
                for d in _sessions(self.n_bars) if start <= d <= end]

    def fetch_fundamentals(self, symbol):
        self.calls.append("fundamentals")
        return {"General": {"CurrencyCode": "USD"},
                "Highlights": {"MarketCapitalization": 123456789}}


def test_unknown_symbol_full_lifecycle(app_db, cleanup_symbols, monkeypatch):
    s = app_db
    cleanup_symbols.append("ZQAN")
    gate_fetch, gate_desk = threading.Event(), threading.Event()
    adapter = StubAdapter(gate=gate_fetch)
    captured: dict[str, object] = {}

    def fake_desk(session, clock, symbols, source=None):
        captured["symbols"], captured["source"] = symbols, source
        assert gate_desk.wait(timeout=15)
        return DeskReport(memos=(DeskMemo(symbols[0], "WATCHLIST", "LOW"),))

    monkeypatch.setattr(analyze, "_build_adapter", lambda sym, exch: adapter)
    monkeypatch.setattr(analyze, "run_desk", fake_desk)

    assert analyze.start_analysis("ZQAN", "investing.com") is True
    st = analyze.analysis_status()
    assert st["running"] is True and st["phase"] == "fetching"
    assert st["symbol"] == "ZQAN" and st["source"] == "investing.com"

    # busy is an answer, not an error — and nothing runs twice
    assert analyze.start_analysis("OTHR", None) is False

    gate_fetch.set()
    _wait(lambda: analyze.analysis_status()["phase"] == "analyzing",
          msg="analyzing phase")
    gate_desk.set()
    _wait_finished()

    st = analyze.analysis_status()
    assert st["phase"] == "done"
    assert st["result"] == {"outcome": "memo", "recommendation": "WATCHLIST",
                            "conviction": "LOW"}
    assert captured == {"symbols": ["ZQAN"], "source": "investing.com"}

    row = s.execute(text(
        "SELECT is_active, exchange, market FROM market.instruments "
        "WHERE symbol = 'ZQAN'")).one()
    assert row.is_active is False          # invisible to scanner/desk-nightly/gates
    assert (row.exchange, row.market) == ("US", "US")   # US-only v1
    n_bars = s.execute(text(
        "SELECT count(*) FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol = 'ZQAN' AND pb.source = 'StubAdapter'")).scalar()
    assert n_bars == 60
    assert s.execute(text(
        "SELECT count(*) FROM market.fundamentals f "
        "JOIN market.instruments i ON i.id = f.instrument_id "
        "WHERE i.symbol = 'ZQAN'")).scalar() == 1
    # the second (busy) request must not have created anything
    assert s.execute(text("SELECT count(*) FROM market.instruments "
                          "WHERE symbol = 'OTHR'")).scalar() == 0


def test_cage_hold_is_reported_done_and_honest(app_db, cleanup_symbols, monkeypatch):
    cleanup_symbols.append("ZQCH")
    monkeypatch.setattr(analyze, "_build_adapter", lambda sym, exch: StubAdapter())
    monkeypatch.setattr(
        analyze, "run_desk",
        lambda session, clock, symbols, source=None: DeskReport(
            cage_holds=((symbols[0], "grounding: two consecutive schema failures"),)))
    assert analyze.start_analysis("ZQCH", None) is True
    _wait_finished()
    st = analyze.analysis_status()
    assert st["phase"] == "done"           # a held cage is the system working
    assert st["result"] == {"outcome": "cage_held",
                            "reason": "grounding: two consecutive schema failures"}
    assert "CAGE HELD" in str(st["detail"])


def test_vendor_failure_is_honest_and_releases_the_lock(app_db, cleanup_symbols,
                                                        monkeypatch):
    cleanup_symbols.append("ZQVF")

    class BoomAdapter(StubAdapter):
        def fetch_splits(self, symbol, start, end):
            raise RuntimeError("vendor down: connect timeout")

    monkeypatch.setattr(analyze, "_build_adapter", lambda sym, exch: BoomAdapter())
    assert analyze.start_analysis("ZQVF", "other") is True
    _wait_finished()
    st = analyze.analysis_status()
    assert st["phase"] == "failed" and "vendor down" in str(st["detail"])
    assert analyze._analysis_lock.locked() is False    # a new run may start


def test_zero_bars_is_a_hard_failure(app_db, cleanup_symbols, monkeypatch):
    cleanup_symbols.append("ZQNB")
    monkeypatch.setattr(analyze, "_build_adapter",
                        lambda sym, exch: StubAdapter(n_bars=0))
    assert analyze.start_analysis("ZQNB", None) is True
    _wait_finished()
    st = analyze.analysis_status()
    assert st["phase"] == "failed"
    assert "no daily bars available for ZQNB" in str(st["detail"])


def test_known_fresh_symbol_makes_no_vendor_calls(app_db, cleanup_symbols,
                                                  monkeypatch):
    """Staleness conventions respected: bars current through the last completed
    session and a fundamentals snapshot within FUNDAMENTALS_STALE_DAYS mean
    the vendor is never touched — the desk just runs."""
    s = app_db
    cleanup_symbols.append("ZQKF")
    s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, name, currency, "
        " is_active) VALUES ('ZQKF', 'NYSE', 'US', 'known fresh', 'USD', TRUE) "
        "ON CONFLICT (symbol, exchange) DO NOTHING"))
    end = last_completed_session("US", datetime.now(UTC))
    s.execute(text(
        "INSERT INTO market.price_bars_daily "
        "(instrument_id, bar_date, open, high, low, close, volume, source) "
        "SELECT id, :d, 10, 11, 9, 10.5, 1000, 'EodhdAdapter' "
        "FROM market.instruments WHERE symbol = 'ZQKF' "
        "ON CONFLICT (instrument_id, bar_date) DO NOTHING"), {"d": end})
    s.execute(text(
        "INSERT INTO market.fundamentals (instrument_id, as_of, payload, source) "
        "SELECT id, CURRENT_DATE, '{}', 'EodhdAdapter' "
        "FROM market.instruments WHERE symbol = 'ZQKF' "
        "ON CONFLICT (instrument_id, as_of) DO NOTHING"))
    s.commit()

    adapter = StubAdapter()
    monkeypatch.setattr(analyze, "_build_adapter", lambda sym, exch: adapter)
    monkeypatch.setattr(
        analyze, "run_desk",
        lambda session, clock, symbols, source=None: DeskReport(
            skipped=((symbols[0], "not enough real bars"),)))
    assert analyze.start_analysis("ZQKF", None) is True
    _wait_finished()
    st = analyze.analysis_status()
    assert st["phase"] == "done"           # an honest skip is a completed analysis
    assert st["result"] == {"outcome": "skipped", "reason": "not enough real bars"}
    assert adapter.calls == []             # zero vendor calls: everything was fresh


def test_status_snapshot_never_leaks_the_live_dict():
    """Same discipline as scheduler.status(): mutating the snapshot must not
    reach the live status (no DB needed)."""
    analyze._status.update(result={"outcome": "memo", "recommendation": "BUY"})
    phase_before = analyze._status["phase"]
    snap = analyze.analysis_status()
    snap["result"]["recommendation"] = "mutated"
    snap["phase"] = "mutated"
    assert analyze._status["result"]["recommendation"] == "BUY"
    assert analyze._status["phase"] == phase_before
    analyze._status.update(phase="idle", symbol=None, source=None, started_at=None,
                           finished_at=None, detail=None, result=None)
