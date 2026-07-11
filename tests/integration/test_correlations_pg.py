"""L8 correlation feed wired against market.price_bars_daily (Doc 04 §3 L8).

atlas_test's fixture data covers only a 4-day window — far below the 60-return
minimum — so this test seeds ~120 sessions of synthetic EodhdAdapter bars
directly via SQL and exercises the real query path: the instruments join, the
source filter, window pinning to the last 90 sessions, no look-ahead past
`end`, and the fail-closed Decimal("1") paths. Nothing is committed: the
pg_session fixture rolls back, so atlas_test is left untouched.

Construction: candidate ZCORRA alternates x1.02 / x0.99 closes. A symbol in
the OPPOSITE phase has returns r_b = 0.01 - r_a (an exact affine relation with
slope -1), so a correctly-windowed correlation is exactly -1.0000 — while
every deliberately planted trap (pre-window same-phase bars, post-end
same-phase bars, fixture-source bars) would drag the value away from -1.0000
if the corresponding filter broke.
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from atlas.dcp.risk.correlations import correlations_with_existing
from tests.conftest import requires_pg

pytestmark = requires_pg

BASE = date(2025, 1, 6)
DATES = [BASE + timedelta(days=i) for i in range(130)]
END = DATES[119]  # indices 120..129 exist only as look-ahead traps


def _closes(phase_for_index) -> list[Decimal]:
    """Closes from alternating multipliers; phase_for_index(i) -> 0 keeps the
    candidate's phase (same returns), 1 swaps it (returns 0.01 - r)."""
    closes, c = [], 100.0
    for i in range(130):
        if i:
            c *= 1.02 if (i + phase_for_index(i)) % 2 else 0.99
        closes.append(Decimal(str(round(c, 6))))
    return closes


def _seed(s, symbol, bars, source="EodhdAdapter"):
    iid = s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency) "
        "VALUES (:sym, 'XTEST', 'US', 'stock', :sym, 'USD') RETURNING id"),
        {"sym": symbol}).scalar()
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, close, source) "
        "VALUES (:iid, :d, :c, :src)"),
        [{"iid": iid, "d": d, "c": c, "src": source} for d, c in bars])


def test_wired_l8_feed_windows_sources_and_fails_closed(pg_session):
    s = pg_session
    # defensive: a previously aborted run can never have committed, but keep
    # the seed idempotent anyway (all inside the rolled-back transaction)
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZCORR%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZCORR%'"))

    a = _closes(lambda i: 0)
    # B: same phase for the 30 oldest bars (outside the 90-session window),
    # OPPOSITE phase inside the window (indices 31..119), and same phase again
    # after `end` — window pinning and no-look-ahead traps in one series.
    b = _closes(lambda i: 1 if 31 <= i <= 119 else 0)
    c = _closes(lambda i: 1)

    _seed(s, "ZCORRA", list(zip(DATES, a, strict=True)))
    _seed(s, "ZCORRB", list(zip(DATES, b, strict=True)))
    # C: only 40 sessions of history -> 39 overlapping returns < 60 minimum
    _seed(s, "ZCORRC", list(zip(DATES[80:120], c[80:120], strict=True)))
    # D: 120 anti-phase sessions but fixture-sourced -> must be invisible
    _seed(s, "ZCORRD", list(zip(DATES[:120], c[:120], strict=True)),
          source="FixtureAdapter")

    result = correlations_with_existing(
        s, "ZCORRA", ["ZCORRB", "ZCORRC", "ZCORRD", "ZCORRE"], end=END)

    # exactly -1.0000 proves: 90-session window (the 30 same-phase old bars
    # excluded), no look-ahead (post-end same-phase bars excluded), source
    # filter and symbol join intact
    assert result["ZCORRB"] == Decimal("-1.0000")
    # thin overlap fails CLOSED to worst case, never omitted (anti-phase data
    # would read -1.0000 if it were computed)
    assert result["ZCORRC"] == Decimal("1")
    # fixture bars are not vendor data: no usable window -> fail closed
    assert result["ZCORRD"] == Decimal("1")
    # unknown symbol entirely: fail closed, still present in the result
    assert result["ZCORRE"] == Decimal("1")

    # positive control: widening the window to 120 sessions pulls the 30
    # same-phase bars back in, so the correlation must leave -1.0000 — the
    # 90-session default above really did pin the window
    mixed = correlations_with_existing(s, "ZCORRA", ["ZCORRB"], end=END,
                                       window_sessions=120)["ZCORRB"]
    assert Decimal("-1") < mixed < Decimal("0")
