"""Memo freshness (derived, never stored) + the open-BUY-calls live preview.

The staleness contract: memos are append-only records, so `freshness` is
recomputed against the latest desk output on EVERY read — each new cycle
automatically expires whatever it did not re-affirm:

  * current    — produced on the latest desk-output day;
  * superseded — a NEWER memo exists for the same symbol (points at it);
  * expired    — the desk has run since and did NOT re-affirm the symbol.

And /memos/performance marks every non-shadow BUY to market vs SPY on the AUD
reporting basis — labelled unrealized/informal (the scorecard is the verdict).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

D1 = datetime(2026, 7, 28, 23, 30, tzinfo=UTC)
D3 = datetime(2026, 8, 4, 23, 30, tzinfo=UTC)     # the latest desk output day


def _memo(s, sym: str, rec: str, at: datetime) -> None:
    s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, recommendation, "
        " conviction, thesis, kill_criteria, evidence_refs, dissent, created_at) "
        "VALUES ('committee', :sym, :rec, 'LOW', 'thesis', '[]', '[]', 'dissent', :at)"),
        {"sym": sym, "rec": rec, "at": at})


@pytest.fixture
def client(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    clean_audit.execute(text("DELETE FROM research.memo_outcomes"))
    clean_audit.execute(text("DELETE FROM research.memo_reviews"))
    clean_audit.execute(text("DELETE FROM research.memos"))
    clean_audit.commit()
    yield TestClient(app), clean_audit
    clean_audit.execute(text("DELETE FROM research.memo_outcomes"))
    clean_audit.execute(text("DELETE FROM research.memo_reviews"))
    clean_audit.execute(text("DELETE FROM research.memos"))
    clean_audit.commit()
    reset_app_engine()


def test_freshness_current_superseded_expired(client):
    c, s = client
    _memo(s, "AAA", "BUY", D1)          # re-affirmed later -> superseded
    _memo(s, "AAA", "WATCHLIST", D3)    # latest day -> current
    _memo(s, "BBB", "BUY", D1)          # never re-affirmed -> expired
    _memo(s, "CCC", "BUY", D3)          # latest day -> current
    s.commit()
    rows = c.get("/v1/research/memos?limit=25").json()
    by = {(r["instrument_symbol"], r["created_at"][:10]): r for r in rows}

    assert by[("AAA", "2026-08-04")]["freshness"] == "current"
    assert by[("CCC", "2026-08-04")]["freshness"] == "current"

    old_aaa = by[("AAA", "2026-07-28")]
    assert old_aaa["freshness"] == "superseded"
    assert old_aaa["superseded_by_rec"] == "WATCHLIST"
    assert old_aaa["superseded_by_date"] == "2026-08-04"

    bbb = by[("BBB", "2026-07-28")]
    assert bbb["freshness"] == "expired"          # desk ran since, no re-affirm
    assert bbb["age_days"] == (D3.date() - D1.date()).days
    assert bbb["superseded_by_rec"] is None


def test_a_new_cycle_expires_yesterdays_unaffirmed_buys(client):
    """The 'expire on each and every cycle run' contract: BEFORE a newer desk
    run exists, a BUY is current; the moment a later run lands WITHOUT
    re-affirming it, the SAME memo reads expired — no mutation, pure
    derivation, so it can never miss a cycle."""
    c, s = client
    _memo(s, "DDD", "BUY", D1)
    s.commit()
    assert c.get("/v1/research/memos").json()[0]["freshness"] == "current"
    _memo(s, "EEE", "BUY", D3)          # the next cycle's output, DDD absent
    s.commit()
    rows = {r["instrument_symbol"]: r for r in c.get("/v1/research/memos").json()}
    assert rows["DDD"]["freshness"] == "expired"
    assert rows["EEE"]["freshness"] == "current"


def test_performance_endpoint_marks_buys_to_market(client):
    c, s = client
    # flat USD->AUD so returns cancel through the reporting basis (the
    # source-picks test convention)
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD', DATE '2020-01-01', '1.0', 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = '1.0'"))
    for sym, drift in (("SPY", 1.000), ("ZBUY", 1.01)):   # ZBUY outruns a flat SPY
        s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                       "(SELECT id FROM market.instruments WHERE symbol=:s)"), {"s": sym})
        s.execute(text("DELETE FROM market.instruments WHERE symbol=:s"), {"s": sym})
        iid = s.execute(text(
            "INSERT INTO market.instruments (symbol, exchange, market, instrument_type,"
            " name, currency, is_active) VALUES (:s,'US','US','stock',:s,'USD',true) "
            "RETURNING id"), {"s": sym}).scalar()
        d, px = date(2026, 7, 20), 100.0
        for i in range(12):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            s.execute(text(
                "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open,"
                " high, low, close, volume, source) "
                "VALUES (:i,:d,:c,:c,:c,:c,1000,'EodhdAdapter')"),
                {"i": iid, "d": d, "c": round(px, 6)})
            d += timedelta(days=1)
            px *= drift
    _memo(s, "ZBUY", "BUY", datetime(2026, 7, 24, 23, 30, tzinfo=UTC))
    s.commit()
    body = c.get("/v1/research/memos/performance").json()
    assert "unrealized" in body["note"]                    # honesty on the wire
    call = next(x for x in body["calls"] if x["symbol"] == "ZBUY")
    assert call["memo_date"] == "2026-07-24"
    assert call["sessions"] > 0
    assert call["excess"] is not None and call["excess"] > 0   # 1%/day vs flat SPY
