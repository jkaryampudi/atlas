"""Split adjustment on the three desk read paths (desk-review 2026-07 item 2):
build_evidence, scanner v1, and the scorecard (both legs). Vendor bars are
stored RAW, so before this fix a 10:1 split inside any window fabricated a
phantom -90% move — grounded memos on false evidence, hijacked scanner ranks,
and false outcomes written to the append-only memo_outcomes.

Each test seeds the 10:1 mid-window fixture, states the RAW value the old
code would have computed, and asserts the adjusted truth. Volumes are checked
on the scanner (adjustment.py convention: pre-split volume multiplied by the
ratio). All seeding is txn-local (pg_session rolls back).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.agents.live_run import build_evidence
from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.scanner.v1 import scan
from atlas.dcp.scorecard import compute_memo_outcomes
from tests.conftest import requires_pg

pytestmark = requires_pg


def _instrument(s, symbol: str, *, active: bool = True) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'Information Technology', "
        "'USD', :act) RETURNING id"), {"sym": symbol, "act": active}).scalar())


def _bars(s, iid: str, dates, closes, volumes) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, :v, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": c, "v": v}
         for d, c, v in zip(dates, closes, volumes)])


def _split(s, iid: str, on: date, ratio: int) -> None:
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_date, "
        "action_type, ratio, source) VALUES (:iid, :d, 'split', :r, 'test')"),
        {"iid": iid, "d": on, "r": ratio})


def _weekdays(n: int, start: date) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _weekdays_ending(n: int, end: date) -> list[date]:
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


# --------------------------------------------------------- build_evidence

def _seed_evidence_series(s, symbol: str, *, split_at_idx: int | None,
                          split_on: date | None = None) -> list[date]:
    """60 bars ending 2026-07-10: raw close 1000 before the split index, 100
    after (a real 10:1 split leaves the true price flat at 100); volume flat."""
    dates = _weekdays_ending(60, date(2026, 7, 10))
    assert dates[-1] == date(2026, 7, 10)
    iid = _instrument(s, symbol)
    if split_at_idx is not None:
        closes = [1000.0] * split_at_idx + [100.0] * (60 - split_at_idx)
        _split(s, iid, dates[split_at_idx], 10)
    else:
        closes = [100.0] * 60
        if split_on is not None:            # recorded but out-of-window split
            _split(s, iid, split_on, 10)
    _bars(s, iid, dates, closes, [1000] * 60)
    return dates


def test_build_evidence_mid_window_split_raw_would_have_lied(clean_audit):
    s = clean_audit
    _seed_evidence_series(s, "ZEVD", split_at_idx=45)   # inside the 20d window
    evidence = build_evidence(s, "ZEVD")
    bars_body, ind_body = evidence[0][1], evidence[1][1]
    # RAW would have read: "20 sessions ago 1000.00" -> a phantom -90% move
    assert "1000.00" not in bars_body
    assert "20 sessions ago 100.00" in bars_body
    assert "latest close 100.00" in bars_body
    # indicators over the adjusted series: flat, not the raw SMA20 of 325.00
    assert ind_body == ("DCP indicators for ZEVD as of 2026-07-10: "
                        "SMA20 100.00, SMA50 100.00, 20-day return 0.00 "
                        "percent, last close 100.00.")


def test_build_evidence_future_recorded_split_is_not_applied(clean_audit):
    """No look-ahead: a split dated after the last bar must not reshape the
    evidence window (raw application would divide every close by 10)."""
    s = clean_audit
    _seed_evidence_series(s, "ZEVF", split_at_idx=None,
                          split_on=date(2026, 7, 20))
    bars_body = build_evidence(s, "ZEVF")[0][1]
    assert "latest close 100.00" in bars_body
    assert "10.00" not in bars_body


# ----------------------------------------------------------------- scanner

T_SCAN = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)     # XNYS session closed
SCAN_SESSIONS = trading_days_between("US", date(2026, 4, 1),
                                     date(2026, 7, 15))[-60:]


def test_scanner_split_neutralizes_phantom_return_and_volume_surge(clean_audit):
    s = clean_audit
    # private universe: park every other instrument (txn-local, rolled back)
    s.execute(text("UPDATE market.instruments SET is_active = false"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks",
              "trading.trade_proposals", "trading.positions"):
        s.execute(text(f"DELETE FROM {t}"))
    zspl = _instrument(s, "ZSPL")
    _bars(s, zspl, SCAN_SESSIONS,
          [1000.0] * 45 + [100.0] * 15,        # raw: a -90% cliff at the split
          [1000] * 45 + [10000] * 15)          # raw: a 10x volume "surge"
    _split(s, zspl, SCAN_SESSIONS[45], 10)
    zflt = _instrument(s, "ZFLT")
    _bars(s, zflt, SCAN_SESSIONS, [100.0] * 60, [10000] * 60)

    report = scan(s, FrozenClock(T_SCAN), top_n=2)
    comp = {e.symbol: e.components for e in report.shortlist}
    assert set(comp) == {"ZFLT", "ZSPL"}
    zspl_c = comp["ZSPL"]
    assert zspl_c is not None
    # RAW would have scored |100/1000 - 1| = 0.9 and surge 10000/3250 ≈ 3.077
    assert zspl_c.ret20_abs == pytest.approx(0.0)
    assert zspl_c.volume_surge == pytest.approx(1.0)   # 10000 / (1000*10 -> 10000)
    # ...identical to the genuinely-flat instrument: nothing HAPPENED here
    zflt_c = comp["ZFLT"]
    assert zflt_c is not None
    assert (zflt_c.ret20_abs, zflt_c.volume_surge) == (
        pytest.approx(0.0), pytest.approx(1.0))


# --------------------------------------------------------------- scorecard

OUT_SESSIONS = _weekdays(70, date(2026, 1, 5))
ANCHOR_IDX = 5


def _at(d: date, hour: int = 21) -> datetime:
    return datetime.combine(d, time(hour, 0), tzinfo=UTC)


def test_scorecard_split_on_both_legs_no_phantom_outcome(clean_audit):
    s = clean_audit
    # private SPY (the resolver demands exactly one active instrument)
    s.execute(text("UPDATE market.instruments SET is_active = false "
                   "WHERE symbol = 'SPY'"))
    zout = _instrument(s, "ZOUT")
    spy = _instrument(s, "SPY")
    # instrument leg: 10:1 split between anchor (idx 5) and forward (idx 25)
    _bars(s, zout, OUT_SESSIONS,
          [1000.0] * 15 + [100.0] * 55, [1000] * 70)
    _split(s, zout, OUT_SESSIONS[15], 10)
    # benchmark leg: 2:1 SPY split in the same window
    _bars(s, spy, OUT_SESSIONS,
          [100.0] * 10 + [50.0] * 60, [1000] * 70)
    _split(s, spy, OUT_SESSIONS[10], 2)
    s.execute(text(
        "INSERT INTO research.memos (memo_type, instrument_symbol, "
        "recommendation, evidence_refs, created_at) "
        "VALUES ('committee', 'ZOUT', 'REJECT', '[]', :ca)"),
        {"ca": _at(OUT_SESSIONS[ANCHOR_IDX])})

    report = compute_memo_outcomes(s, FrozenClock(_at(OUT_SESSIONS[69], 23)))
    by_h = {r.horizon_sessions: r for r in report.written}
    assert set(by_h) == {20, 60}
    r20 = by_h[20]
    # RAW would have written fwd_return -0.900000 (and spy_return -0.500000):
    # a fabricated outcome in an append-only table. Adjusted, both legs are
    # flat — the truth is "nothing happened".
    assert r20.anchor_close == Decimal("100")          # split-adjusted anchor
    assert r20.fwd_close == Decimal("100")
    assert r20.fwd_return == Decimal("0.000000")
    assert r20.spy_return == Decimal("0.000000")
    assert r20.excess == Decimal("0.000000")
    row = s.execute(text(
        "SELECT anchor_close, fwd_return, spy_return, excess "
        "FROM research.memo_outcomes WHERE horizon_sessions = 20")).one()
    assert row.fwd_return == Decimal("0.000000")
    assert row.spy_return == Decimal("0.000000")
    assert row.excess == Decimal("0.000000")
