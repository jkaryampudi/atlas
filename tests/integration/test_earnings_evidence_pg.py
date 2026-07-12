"""Earnings evidence block (desk-review memo 2026-07 item 9): hand-pinned
renders — ISO dates and session counts only, zero vendor prose — plus the
desk wiring: build_evidence appends the new blocks when (and only when) the
records exist, and every digit in a pinned body is a standalone token the
grounding verifier can match verbatim."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.agents.live_run import build_evidence
from atlas.agents.runtime.grounding import corpus_numeric_tokens
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.earnings import extract_earnings_evidence
from atlas.dcp.market_data.ingest import seed_instruments
from tests.conftest import requires_pg

pytestmark = requires_pg
SEEDS = Path(__file__).parents[2] / "seeds" / "instruments_seed.csv"
ON = date(2026, 7, 10)                       # a Friday; next XNYS session is Mon 13th
FETCHED = datetime(2026, 7, 13, 2, 0, tzinfo=UTC)

GOLDEN_BOTH = ("Earnings calendar for ZERN: next scheduled report 2026-07-24 "
               "(10 sessions after 2026-07-10). Last report 2026-04-23.")
GOLDEN_NEXT_ONLY = ("Earnings calendar for ZERN: next scheduled report 2026-07-13 "
                    "(1 session after 2026-07-10). No earlier report on record.")
GOLDEN_LAST_ONLY = ("Earnings calendar for ZERN: no scheduled report on record "
                    "after 2026-07-10. Last report 2026-04-23.")


@pytest.fixture
def clean_calendar(pg_session):
    s = pg_session
    seed_instruments(s, SEEDS)
    s.execute(text("DELETE FROM market.earnings_calendar"))
    s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) VALUES "
        "('ZERN', 'XTEST', 'US', 'stock', 'Earnings Evidence Corp', 'USD') "
        "ON CONFLICT (symbol, exchange) DO NOTHING"))
    yield s


def _row(s, symbol: str, report_date: date, fetched_at: datetime = FETCHED) -> None:
    s.execute(text(
        "INSERT INTO market.earnings_calendar "
        "(instrument_id, report_date, when_time, fetched_at, source) "
        "SELECT i.id, :d, NULL, :fa, 'test' FROM market.instruments i "
        "WHERE i.symbol = :sym"),
        {"d": report_date, "fa": fetched_at, "sym": symbol})


def test_next_and_last_report_hand_pinned(clean_calendar):
    s = clean_calendar
    _row(s, "ZERN", date(2026, 4, 23))
    _row(s, "ZERN", date(2026, 7, 24))
    got = extract_earnings_evidence(s, "ZERN", on=ON)
    assert got == ("dcp:earnings:ZERN:2026-07-13", GOLDEN_BOTH)
    # grounding compatibility: every numeric the desk may quote is a
    # standalone verbatim token in the body
    tokens = corpus_numeric_tokens(got[1])
    assert {"2026", "07", "24", "10", "04", "23"} <= tokens


def test_next_only_singular_session_count(clean_calendar):
    s = clean_calendar
    _row(s, "ZERN", date(2026, 7, 13))       # the very next session: 1 session, not 1 sessions
    got = extract_earnings_evidence(s, "ZERN", on=ON)
    assert got == ("dcp:earnings:ZERN:2026-07-13", GOLDEN_NEXT_ONLY)


def test_last_only_when_nothing_scheduled(clean_calendar):
    s = clean_calendar
    _row(s, "ZERN", date(2026, 4, 23))
    got = extract_earnings_evidence(s, "ZERN", on=ON)
    assert got == ("dcp:earnings:ZERN:2026-07-13", GOLDEN_LAST_ONLY)


def test_no_record_returns_none_never_a_fabricated_line(clean_calendar):
    assert extract_earnings_evidence(clean_calendar, "ZERN", on=ON) is None
    assert extract_earnings_evidence(clean_calendar, "NOPE-99", on=ON) is None


def test_ref_pins_the_newest_fetch_date(clean_calendar):
    s = clean_calendar
    _row(s, "ZERN", date(2026, 4, 23), fetched_at=datetime(2026, 5, 1, tzinfo=UTC))
    _row(s, "ZERN", date(2026, 7, 24), fetched_at=FETCHED)
    got = extract_earnings_evidence(s, "ZERN", on=ON)
    assert got is not None
    assert got[0] == "dcp:earnings:ZERN:2026-07-13"   # newest fetched_at wins


# ------------------------------------------------------------- desk wiring

def _seed_desk_instrument(s, symbol: str) -> date:
    """An instrument with 60 vendor bars ending 2026-07-10 (build_evidence
    needs >= 51 EodhdAdapter-sourced bars)."""
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', 'Desk Wiring Corp', 'USD') "
        "RETURNING id"), {"sym": symbol}).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "VALUES (:iid, :d, :px, :px, :px, :px, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": ON - timedelta(days=59 - i), "px": 100 + i * 0.5}
         for i in range(60)])
    return ON


def _seed_spy_bars(s, n: int = 150) -> None:
    """SPY vendor bars ending 2026-07-10, gently trending up: enough history
    for a post-warmup regime label, and the label is bull."""
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id = "
                   "(SELECT id FROM market.instruments WHERE symbol = 'SPY')"))
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "SELECT i.id, :d, :px, :px, :px, :px, 1000, 'EodhdAdapter' "
        "FROM market.instruments i WHERE i.symbol = 'SPY'"),
        [{"d": ON - timedelta(days=n - 1 - i), "px": 400 + i * 0.5}
         for i in range(n)])


def _scanner_event(s, symbol: str) -> None:
    PostgresAuditLog(s, FrozenClock(FETCHED)).append(
        event_type="scanner.completed", entity_type="scanner",
        entity_id="2026-07-13", actor_type="dcp", actor_id="scanner_v1",
        payload={"criteria_version": "1.0", "top_n": 5,
                 "sessions": {"US": ON.isoformat(), "AU": ON.isoformat()},
                 "scanned": 112, "eligible": 108, "ineligible": 4,
                 "shortlist": [{"symbol": symbol, "held": False, "score": 1.83,
                                "ret20_abs": 0.142, "ret20_rank": 0.95,
                                "volume_surge": 1.51, "surge_rank": 0.88}]})


def test_build_evidence_appends_new_blocks_when_records_exist(clean_calendar):
    s = clean_calendar
    last = _seed_desk_instrument(s, "ZERW")
    _row(s, "ZERW", date(2026, 7, 24))
    _row(s, "ZERW", date(2026, 4, 23))
    _seed_spy_bars(s)
    _scanner_event(s, "ZERW")
    evidence = build_evidence(s, "ZERW")
    refs = [ref for ref, _ in evidence]
    assert refs == [f"dcp:bars:ZERW:{last.isoformat()}",
                    f"dcp:indicators:ZERW:{last.isoformat()}",
                    "dcp:quant:verdicts:v1:ZERW",
                    "dcp:earnings:ZERW:2026-07-13",     # no fundamentals seeded:
                    f"dcp:regime:v1:SPY:{last.isoformat()}",   # blocks omit
                    f"dcp:scanner:1.0:ZERW:{last.isoformat()}"]  # independently
    bodies = dict(evidence)
    assert "2026-07-24" in bodies["dcp:earnings:ZERW:2026-07-13"]
    assert bodies[f"dcp:regime:v1:SPY:{last.isoformat()}"] == (
        "Market regime (deterministic classifier v1, SPY benchmark): "
        "bull as of 2026-07-10.")
    assert "attention, not prediction" in bodies[f"dcp:scanner:1.0:ZERW:{last.isoformat()}"]


def test_build_evidence_omits_all_blocks_when_records_absent(clean_calendar):
    s = clean_calendar
    # no earnings rows, no SPY history, no scanner event for this symbol
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id = "
                   "(SELECT id FROM market.instruments WHERE symbol = 'SPY')"))
    last = _seed_desk_instrument(s, "ZERX")
    evidence = build_evidence(s, "ZERX")
    assert [ref for ref, _ in evidence] == [
        f"dcp:bars:ZERX:{last.isoformat()}",
        f"dcp:indicators:ZERX:{last.isoformat()}",
        "dcp:quant:verdicts:v1:ZERX"]
