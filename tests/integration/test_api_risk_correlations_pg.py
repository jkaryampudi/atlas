"""L8 CORRELATION HEAT MATRIX over HTTP (GET /v1/risk/correlations): pinned
pairwise values on seeded vendor bars, null (never fake) on thin data, the
default book+scanner symbol set, unknown-symbol reporting, and the 12-symbol
cap.

Construction reuses test_correlations_pg's exact-affine trick: a candidate
alternating x1.02 / x0.99 closes vs an opposite-phase series gives correlation
exactly -1.0000, and an identical-phase series exactly +1.0000 — while
deliberately planted post-end same-phase bars would drag the -1 away if the
clock-date look-ahead cap broke.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from atlas.api.main import app
from atlas.api.routers import risk as risk_router
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

BASE = date(2025, 1, 6)
DATES = [BASE + timedelta(days=i) for i in range(130)]
END = DATES[119]                  # indices 120..129 exist only as look-ahead traps
NOW = datetime(END.year, END.month, END.day, 20, 0, tzinfo=UTC)


def _closes(phase_for_index) -> list[Decimal]:
    closes, c = [], 100.0
    for i in range(130):
        if i:
            c *= 1.02 if (i + phase_for_index(i)) % 2 else 0.99
        closes.append(Decimal(str(round(c, 6))))
    return closes


def _seed(s, symbol, bars) -> str:
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'USD') RETURNING id"),
        {"sym": symbol}).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, close, source) "
        "VALUES (:iid, :d, :c, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": c} for d, c in bars])
    return str(iid)


def _wipe(s) -> None:
    s.execute(text("DELETE FROM trading.positions WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZCM%')"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZCM%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZCM%'"))


@pytest.fixture
def cclient(monkeypatch, clean_audit):
    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    clock = FrozenClock(NOW)
    monkeypatch.setattr(risk_router, "_clock", lambda: clock)

    s = clean_audit
    _wipe(s)
    a = _closes(lambda i: 0)
    # anti-phase through END, SAME phase after it: if the clock-date cap ever
    # leaked future bars into the window, the -1.0000 pin below would break
    b = _closes(lambda i: 1 if i <= 119 else 0)
    ids = {"ZCMA": _seed(s, "ZCMA", list(zip(DATES, a, strict=True))),
           "ZCMB": _seed(s, "ZCMB", list(zip(DATES, b, strict=True))),
           "ZCMC": _seed(s, "ZCMC", list(zip(DATES, a, strict=True)))}
    # ZCMD: 40 sessions -> 39 overlapping returns < the 60-return minimum
    ids["ZCMD"] = _seed(s, "ZCMD", list(zip(DATES[80:120], a[80:120], strict=True)))
    s.commit()

    yield TestClient(app), s, clock, ids
    _wipe(s)
    s.commit()
    reset_app_engine()


def test_explicit_symbols_pin_values_null_on_thin_and_report_unknown(cclient):
    c, _, _, _ = cclient
    r = c.get("/v1/risk/correlations?symbols=ZCMA,ZCMB,ZCMC,ZCMD,ZCMNOPE")
    assert r.status_code == 200
    d = r.json()
    assert d["symbols"] == ["ZCMA", "ZCMB", "ZCMC", "ZCMD"]
    assert d["unknown"] == ["ZCMNOPE"]
    assert d["window_sessions"] == 90
    assert d["end"] == END.isoformat()
    assert d["capped"] is False

    m = d["matrix"]
    i = {sym: k for k, sym in enumerate(d["symbols"])}
    # pinned: exact affine anti-phase / same-phase relations (no look-ahead)
    assert m[i["ZCMA"]][i["ZCMB"]] == -1.0
    assert m[i["ZCMA"]][i["ZCMC"]] == 1.0
    assert m[i["ZCMB"]][i["ZCMC"]] == -1.0
    # thin data -> null, never a fake number (the ENGINE would gate these as 1)
    assert m[i["ZCMA"]][i["ZCMD"]] is None
    assert m[i["ZCMB"]][i["ZCMD"]] is None
    assert m[i["ZCMD"]][i["ZCMD"]] is None      # no usable window even vs itself
    # symmetric, with a real diagonal where data exists
    for sym in ("ZCMA", "ZCMB", "ZCMC"):
        assert m[i[sym]][i[sym]] == 1.0
    for a_ in range(4):
        for b_ in range(4):
            assert m[a_][b_] == m[b_][a_]


def test_default_set_is_positions_plus_latest_scanner_shortlist(cclient):
    c, s, clock, ids = cclient
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at) VALUES (:iid, 10, 100, 'USD', :at)"),
        {"iid": ids["ZCMA"], "at": NOW - timedelta(days=30)})
    # two scans on the chain; only the LATEST shortlist counts (ZCMA also
    # shortlisted -> deduplicated behind its book slot). A separate forward-
    # only clock stamps the appends; the API keeps the fixture clock at NOW.
    audit_clock = FrozenClock(NOW - timedelta(hours=2))
    audit = PostgresAuditLog(s, audit_clock)
    for at_offset, shortlist in [(2, ["ZCMD"]),
                                 (1, ["ZCMB", "ZCMC", "ZCMA"])]:
        audit_clock.advance_to(NOW - timedelta(hours=at_offset))
        audit.append(event_type="scanner.completed", entity_type="scanner",
                     entity_id=END.isoformat(), actor_type="dcp",
                     actor_id="scanner_v1",
                     payload={"shortlist": [{"symbol": x} for x in shortlist]})
    s.commit()

    d = c.get("/v1/risk/correlations").json()
    assert d["source"] == "book+scanner"
    assert d["symbols"] == ["ZCMA", "ZCMB", "ZCMC"]   # book first, then shortlist
    assert d["unknown"] == []
    m = d["matrix"]
    assert m[0][1] == -1.0 and m[0][2] == 1.0


def test_default_set_with_empty_book_and_no_scan_is_empty_not_error(cclient):
    c, _, _, _ = cclient
    d = c.get("/v1/risk/correlations").json()
    assert d["symbols"] == [] and d["matrix"] == [] and d["unknown"] == []


def test_cap_at_twelve_symbols_explicit(cclient):
    c, _, _, _ = cclient
    fakes = [f"ZX{k}" for k in range(1, 10)]          # ZX1..ZX9
    q = ",".join(["ZCMA", "ZCMB", "ZCMC", "ZCMD"] + fakes)   # 13 requested
    d = c.get(f"/v1/risk/correlations?symbols={q}").json()
    assert d["capped"] is True
    assert d["symbols"] == ["ZCMA", "ZCMB", "ZCMC", "ZCMD"]
    assert d["unknown"] == fakes[:8]                  # first 12 kept, ZX9 dropped
    assert "ZX9" not in d["unknown"]
    assert len(d["matrix"]) == 4                      # unknowns never render rows
