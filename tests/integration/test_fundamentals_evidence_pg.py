"""Fundamentals evidence extraction against the real jsonb round trip, and
the desk wiring: build_evidence appends the fundamentals tuple when (and only
when) a snapshot exists as of the evidence date.

The golden bodies are shared with the unit suite (single source of truth):
equality here proves Postgres jsonb storage does not perturb a single digit
of what the grounding verifier will match against."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.agents.live_run import build_evidence
from atlas.dcp.market_data.fundamentals import extract_fundamentals_evidence
from tests.conftest import requires_pg
from tests.unit.test_fundamentals_extraction import GOLDEN_AVGO, GOLDEN_SPY

pytestmark = requires_pg
FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "fundamentals"
AS_OF = date(2026, 7, 10)


@pytest.fixture
def clean_fundamentals(pg_session):
    pg_session.execute(text("DELETE FROM market.fundamentals"))
    yield pg_session


def _store(s, symbol: str, as_of: date, payload: dict | None = None) -> None:
    doc = payload if payload is not None else json.loads(
        (FIXTURES / f"{symbol}.json").read_text())
    s.execute(text(
        "INSERT INTO market.fundamentals (instrument_id, as_of, payload, source) "
        "SELECT i.id, :d, CAST(:p AS jsonb), 'test' FROM market.instruments i "
        "WHERE i.symbol = :sym"), {"d": as_of, "p": json.dumps(doc), "sym": symbol})


def test_stock_extraction_golden_pin_through_jsonb(clean_fundamentals):
    s = clean_fundamentals
    _store(s, "AVGO", AS_OF)
    got = extract_fundamentals_evidence(s, "AVGO", on=AS_OF)
    assert got == (f"dcp:fundamentals:AVGO:{AS_OF.isoformat()}", GOLDEN_AVGO)
    # the hostile payload went into the database whole; the evidence body
    # (already proven equal to the golden) carries none of it
    assert "ignore" not in got[1].lower()


def test_etf_extraction_golden_pin_through_jsonb(clean_fundamentals):
    s = clean_fundamentals
    _store(s, "SPY", AS_OF)
    got = extract_fundamentals_evidence(s, "SPY", on=AS_OF)
    assert got == (f"dcp:fundamentals:SPY:{AS_OF.isoformat()}", GOLDEN_SPY)


def test_no_snapshot_returns_none_never_a_fabricated_line(clean_fundamentals):
    assert extract_fundamentals_evidence(clean_fundamentals, "AVGO", on=AS_OF) is None


def test_snapshot_after_the_evidence_date_is_invisible(clean_fundamentals):
    s = clean_fundamentals
    _store(s, "AVGO", AS_OF)
    # a snapshot fetched on 07-10 did not exist on 07-09: no look-ahead
    assert extract_fundamentals_evidence(s, "AVGO", on=AS_OF - timedelta(days=1)) is None


def test_latest_snapshot_at_or_before_on_wins(clean_fundamentals):
    s = clean_fundamentals
    _store(s, "AVGO", date(2026, 7, 1), payload={"marker": True})
    _store(s, "AVGO", AS_OF)
    got = extract_fundamentals_evidence(s, "AVGO", on=date(2026, 7, 5))
    assert got is not None
    assert got[0] == "dcp:fundamentals:AVGO:2026-07-01"   # not the newer 07-10 one
    got = extract_fundamentals_evidence(s, "AVGO", on=AS_OF)
    assert got is not None and got[0] == f"dcp:fundamentals:AVGO:{AS_OF.isoformat()}"


# ------------------------------------------------------------- desk wiring

def _seed_desk_instrument(s, symbol: str) -> date:
    """An instrument with 60 vendor bars ending 2026-07-10 (build_evidence
    needs >= 51 EodhdAdapter-sourced bars)."""
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', 'Desk Wiring Corp', 'USD') "
        "RETURNING id"), {"sym": symbol}).scalar()
    last = AS_OF
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "VALUES (:iid, :d, :px, :px, :px, :px, 1000000, 'EodhdAdapter')"),
        [{"iid": iid, "d": last - timedelta(days=59 - i), "px": 100 + i * 0.5}
         for i in range(60)])
    return last


def test_build_evidence_appends_fundamentals_when_snapshot_exists(clean_fundamentals):
    s = clean_fundamentals
    last = _seed_desk_instrument(s, "ZFND")
    _store(s, "ZFND", last, payload=json.loads((FIXTURES / "AVGO.json").read_text()))
    evidence = build_evidence(s, "ZFND")
    refs = [ref for ref, _ in evidence]
    assert refs == [f"dcp:bars:ZFND:{last.isoformat()}",
                    f"dcp:indicators:ZFND:{last.isoformat()}",
                    "dcp:quant:verdicts:v1:ZFND",
                    f"dcp:fundamentals:ZFND:{last.isoformat()}"]
    body = evidence[3][1]
    assert "market cap 1252470423552" in body
    assert "ignore" not in body.lower()   # the hostile document, defanged


def test_build_evidence_without_snapshot_keeps_current_evidence_set(clean_fundamentals):
    s = clean_fundamentals
    last = _seed_desk_instrument(s, "ZFNE")
    evidence = build_evidence(s, "ZFNE")
    assert [ref for ref, _ in evidence] == [
        f"dcp:bars:ZFNE:{last.isoformat()}",
        f"dcp:indicators:ZFNE:{last.isoformat()}",
        "dcp:quant:verdicts:v1:ZFNE"]
