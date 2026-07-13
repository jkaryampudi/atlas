"""Scanner v1 against a seeded cross-section (atlas/dcp/scanner/v1.py, ADR-0007)
plus the T7 wiring (atlas/ops/daily.py build_scanned_desk).

Hand-verified cross-section, 9 active instruments, clock 2026-07-15 22:00 UTC
(last completed US session = 2026-07-15), 60 seeded sessions each:

  ZSCA  last close 150 (ret .5), 5-session volume 3000  -> score 1 + 5/6
  ZSCB  last close 110 (ret .1), 5-session volume 5000  -> score 2/3 + 1
  ZSCC  last close  60 (ret .4), flat volume            -> score 5/6 + 0
  ZSCD/E/F  flat price + volume                          -> filler
  ZSCP  flat, HELD (open position)                       -> score 1/2 + 2/3
  ZSCG  only 10 stored sessions                          -> ineligible (thin)
  ZSCH  60 sessions ending 2026-07-14                    -> ineligible (stale)

Eligible n = 7, rank denominators 6. Scores: A 11/6, B 5/3, P 7/6, C 5/6,
F 5/6, E 1/2, D 1/6. The shape is deliberate: held ZSCP OUTSCORES ZSCC and
ZSCF, so top-3 correctness proves held names consume no top_n slot, and the
C/F dead heat at 5/6 proves the final-score symbol tiebreak on real rows.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.scanner.v1 import scan
from atlas.ops.daily import build_scanned_desk, run_daily_cycle
from tests.conftest import requires_pg

pytestmark = requires_pg

FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures"

T = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)  # Wed; XNYS 2026-07-15 closed 20:00 UTC
SESSIONS = trading_days_between("US", date(2026, 4, 1), date(2026, 7, 15))[-60:]
STALE_SESSIONS = trading_days_between("US", date(2026, 4, 1), date(2026, 7, 15))[-61:-1]
FLAT = ([100.0] * 60, [1000] * 60)


def _clean(s) -> None:
    """Committed leftovers from aborted runs must never shape a scan
    (mirrors test_daily_cycle_pg._clean)."""
    s.execute(text("UPDATE trading.trade_proposals "
                   "SET risk_check_id = NULL, state = 'draft'"))
    for t in ("trading.tax_lots", "trading.executions", "trading.orders",
              "trading.approvals", "risk.risk_checks", "trading.trade_proposals",
              "trading.positions", "trading.portfolio_snapshots",
              "trading.reconciliations"):
        s.execute(text(f"DELETE FROM {t}"))
    s.execute(text("DELETE FROM workflow.workflow_node_results "
                   "WHERE run_id LIKE 'daily-%'"))
    s.execute(text("DELETE FROM workflow.workflow_runs WHERE run_id LIKE 'daily-%'"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZSC%')"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZSC%'"))


def _instrument(s, symbol: str) -> str:
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        "name, sector_gics, currency) VALUES (:sym, 'XTEST', 'US', 'stock', :sym, "
        "'Information Technology', 'USD') RETURNING id"), {"sym": symbol}).scalar())


def _bars(s, iid: str, dates, closes, volumes) -> None:
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, high, "
        "low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, :v, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": c, "v": v}
         for d, c, v in zip(dates, closes, volumes)])


def _seed_cross_section(s) -> dict[str, str]:
    """The docstring's universe: everything else deactivated (rolled back at
    teardown), 9 ZSC instruments, ZSCP held via an open position with a
    matching open lot (reconciliation-clean for the full-cycle tests)."""
    _clean(s)
    s.execute(text("UPDATE market.instruments SET is_active = false"))
    spec: dict[str, tuple[list[float], list[int]]] = {
        "ZSCA": ([100.0] * 59 + [150.0], [1000] * 55 + [3000] * 5),
        "ZSCB": ([100.0] * 59 + [110.0], [1000] * 55 + [5000] * 5),
        "ZSCC": ([100.0] * 59 + [60.0], FLAT[1]),
        "ZSCD": FLAT, "ZSCE": FLAT, "ZSCF": FLAT, "ZSCP": FLAT,
    }
    ids: dict[str, str] = {}
    for sym, (closes, volumes) in spec.items():
        ids[sym] = _instrument(s, sym)
        _bars(s, ids[sym], SESSIONS, closes, volumes)
    ids["ZSCG"] = _instrument(s, "ZSCG")  # thin: 10 stored sessions
    _bars(s, ids["ZSCG"], SESSIONS[-10:], [100.0] * 10, [1000] * 10)
    ids["ZSCH"] = _instrument(s, "ZSCH")  # stale: 60 sessions ending 2026-07-14
    _bars(s, ids["ZSCH"], STALE_SESSIONS, [100.0] * 60, [1000] * 60)
    pid = s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at, created_at) VALUES (:iid, 10, 100, 'USD', :t, :t) RETURNING id"),
        {"iid": ids["ZSCP"], "t": T}).scalar()
    s.execute(text(
        "INSERT INTO trading.tax_lots (position_id, qty, cost_aud, acquired_at, "
        "created_at) VALUES (:pid, 10, 1500, :t, :t)"), {"pid": pid, "t": T})
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD','2026-07-14',:r,'zsc-test'), "
        "       ('USD','AUD','2026-07-15',:r,'zsc-test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = :r"),
        {"r": Decimal("1.5")})
    return ids


def test_scan_shortlist_counts_and_determinism(clean_audit):
    s = clean_audit
    _seed_cross_section(s)
    report = scan(s, FrozenClock(T), top_n=3)

    assert report.scanned == 9
    assert report.eligible == 7
    assert dict(report.ineligible) == {
        "ZSCG": "thin history: 10 < 60 stored sessions",
        "ZSCH": "stale: latest bar 2026-07-14 < last session 2026-07-15"}
    assert report.sessions == (("US", date(2026, 7, 15)),)

    # top-3 by score, held ZSCP appended WITHOUT consuming a slot — it
    # outscores ZSCC (7/6 > 5/6), so slot-stealing would evict ZSCC here
    assert [(e.symbol, e.held) for e in report.shortlist] == [
        ("ZSCA", False), ("ZSCB", False), ("ZSCC", False), ("ZSCP", True)]
    a, b, c, p = (e.components for e in report.shortlist)
    assert (a.score, b.score) == (pytest.approx(11 / 6), pytest.approx(5 / 3))
    assert a.ret20_abs == pytest.approx(0.5)
    assert a.volume_surge == pytest.approx(18 / 7)
    # ZSCC vs ZSCF is a 5/6 dead heat: the symbol tiebreak picks ZSCC
    assert c.score == pytest.approx(5 / 6)
    assert (c.ret20_rank, c.surge_rank) == (pytest.approx(5 / 6), 0.0)
    # held entries carry their REAL components — the book stays comparable
    assert p.score == pytest.approx(7 / 6)
    assert report.summary() == "scanned 9 · shortlist 3+1 held"

    assert scan(s, FrozenClock(T), top_n=3) == report  # deterministic re-run


def test_held_symbol_ineligible_for_scoring_is_still_shortlisted(clean_audit):
    s = clean_audit
    ids = _seed_cross_section(s)
    s.execute(text(
        "INSERT INTO trading.positions (instrument_id, qty, avg_cost, currency, "
        "opened_at, created_at) VALUES (:iid, 5, 100, 'USD', :t, :t)"),
        {"iid": ids["ZSCG"], "t": T})
    report = scan(s, FrozenClock(T), top_n=3)
    # thin ZSCG cannot be scored — it is STILL on the desk's list (the book
    # outranks every filter) and still honestly counted ineligible
    assert [(e.symbol, e.held) for e in report.shortlist] == [
        ("ZSCA", False), ("ZSCB", False), ("ZSCC", False),
        ("ZSCG", True), ("ZSCP", True)]
    zscg = report.shortlist[3]
    assert zscg.components is None
    assert ("ZSCG", "thin history: 10 < 60 stored sessions") in report.ineligible
    assert report.summary() == "scanned 9 · shortlist 3+2 held"


def test_scanner_completed_audit_event_pinned(clean_audit):
    s = clean_audit
    _seed_cross_section(s)
    scan(s, FrozenClock(T), top_n=3)
    ev = s.execute(text(
        "SELECT entity_type, entity_id, actor_type, actor_id, payload "
        "FROM audit.decision_events WHERE event_type = 'scanner.completed'")
        ).mappings().one()
    assert (ev["entity_type"], ev["entity_id"]) == ("scanner", "2026-07-15")
    assert (ev["actor_type"], ev["actor_id"]) == ("dcp", "scanner_v1")
    # bounded payload: shortlist rows + COUNTS, never all score rows
    assert ev["payload"] == {
        "criteria_version": "1.0",
        "top_n": 3,
        "sessions": {"US": "2026-07-15"},
        "scanned": 9,
        "eligible": 7,
        "ineligible": 2,
        "shortlist": [
            {"symbol": "ZSCA", "held": False, "score": 1.833333, "ret20_abs": 0.5,
             "ret20_rank": 1.0, "volume_surge": 2.571429, "surge_rank": 0.833333},
            {"symbol": "ZSCB", "held": False, "score": 1.666667, "ret20_abs": 0.1,
             "ret20_rank": 0.666667, "volume_surge": 3.75, "surge_rank": 1.0},
            {"symbol": "ZSCC", "held": False, "score": 0.833333, "ret20_abs": 0.4,
             "ret20_rank": 0.833333, "volume_surge": 1.0, "surge_rank": 0.0},
            {"symbol": "ZSCP", "held": True, "score": 1.166667, "ret20_abs": 0.0,
             "ret20_rank": 0.5, "volume_surge": 1.0, "surge_rank": 0.666667},
        ]}


def _capturing_run_desk(captured: dict):
    """A stub desk that records exactly the symbols it was routed (the
    injected-desk pattern from test_daily_cycle_pg.py)."""
    from atlas.agents.desk import DeskReport

    def run_desk(session, clock, symbols):
        captured["symbols"] = list(symbols)
        return DeskReport()
    return run_desk


def test_daily_cycle_routes_exactly_the_shortlist(clean_audit):
    """T7 through the REAL wiring: scan inside the desk node, run_desk gets
    the shortlist and nothing else, and the node line prepends the scan."""
    s = clean_audit
    _seed_cross_section(s)
    captured: dict = {}
    desk = build_scanned_desk(_capturing_run_desk(captured),
                              lambda session: ["UNREACHED"], top_n=3)
    results = run_daily_cycle(s, FrozenClock(T), FixtureAdapter(FIXTURES), desk=desk)

    assert captured["symbols"] == ["ZSCA", "ZSCB", "ZSCC", "ZSCP"]
    # ADR-0010 wiring: the node line now leads with the (empty) signal lane
    # and counts the merged shortlist
    assert results["t7_desk"] == ("signals 0 + scanned 9 -> desk 4 (1 held) "
                                  "· memos 0 (none) "
                                  "· cage holds 0 · spend today $0.00")
    assert results["t6_reconcile"] == "clean"  # the held book stayed recon-clean
    assert "desk FAILED" not in results["t9_report"]
    n = s.execute(text("SELECT count(*) FROM audit.decision_events "
                       "WHERE event_type = 'scanner.completed'")).scalar()
    assert n == 1  # one scan per cycle, on the chain


def test_scan_failure_falls_back_to_full_universe_and_pages(clean_audit, monkeypatch):
    """Fail-soft (ADR-0007): a broken ranker must never blind the desk — it
    falls back to desk_symbols — and must never be silently absorbed: the
    run pages exactly like a desk failure."""
    s = clean_audit
    _seed_cross_section(s)

    def exploding_scan(session, clock, *, top_n=5):
        raise RuntimeError("ranking exploded")

    monkeypatch.setattr("atlas.ops.daily.scan", exploding_scan)
    captured: dict = {}
    desk = build_scanned_desk(_capturing_run_desk(captured),
                              lambda session: ["ZSCA", "ZSCB"], top_n=3)
    results = run_daily_cycle(s, FrozenClock(T), FixtureAdapter(FIXTURES), desk=desk)

    assert captured["symbols"] == ["ZSCA", "ZSCB"]  # the fallback list, not none
    assert results["t7_desk"] == ("scan FAILED (ranking exploded) -> desk full "
                                  "eligible universe · memos 0 (none) · cage holds 0 "
                                  "· spend today $0.00")
    assert results["t3_settle"] == "fills=0"  # trading steps stand untouched
    assert "desk FAILED — see log" in results["t9_report"]
