"""Memo scorecard against the real database (migration 0016): rows written
with hand-pinned numbers, the no-look-ahead maturation flow under an advancing
FrozenClock, the /v1/research/scorecard API shape, and the T9 cycle wiring
(fail-soft, pages like a desk failure). Desk-review 2026-07 item 5 additions:
the analysis-only resolution rule, the bounded bar top-up end to end, and the
dartboard/conviction/source/dissent API surface. Planner math itself is pinned
in tests/unit/test_scorecard.py; this file proves the plumbing end to end.

Seeding follows the house pattern: everything inside the test transaction
(rolled back by pg_session) except the API tests, which must commit for the
TestClient's own connection and therefore clean up explicitly at teardown.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.models import Bar
from atlas.dcp.scorecard import compute_memo_outcomes
from atlas.ops.daily import run_daily_cycle
from tests.conftest import URL, requires_pg, reset_app_engine

pytestmark = requires_pg

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"


def _weekdays(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


SESSIONS = _weekdays(70)                     # Mon 2026-01-05 onward
ANCHOR_IDX = 5                               # the memo's evidence session


def _at(session_date: date, hour: int = 21) -> datetime:
    return datetime.combine(session_date, time(hour, 0), tzinfo=UTC)


def _instrument(s, symbol: str, *, active: bool = True,
                exchange: str = "XTEST") -> str:
    existing = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = :sym "
        "AND exchange = :ex"), {"sym": symbol, "ex": exchange}).scalar()
    if existing is not None:
        s.execute(text("UPDATE market.instruments SET is_active = :act "
                       "WHERE id = :iid"), {"iid": existing, "act": active})
        return str(existing)
    return str(s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, sector_gics, currency, is_active) "
        "VALUES (:sym, :ex, 'US', 'stock', :sym, 'Information Technology', "
        "'USD', :act) RETURNING id"),
        {"sym": symbol, "ex": exchange, "act": active}).scalar())


def _benchmark(s) -> tuple[str, list[str]]:
    """SPY resolution must stay 'exactly one active instrument' (fail-closed),
    and other suites commit their own SPY rows AND bars to atlas_test — so
    these tests park every pre-existing SPY inactive and grade against a
    private XTEST SPY carrying only this file's bars. Rolled back with the
    test transaction; the committed API fixture restores explicitly. Returns
    (private spy id, previously-active spy ids to restore)."""
    prev_active = [str(r.id) for r in s.execute(text(
        "SELECT id FROM market.instruments "
        "WHERE symbol = 'SPY' AND is_active")).all()]
    s.execute(text("UPDATE market.instruments SET is_active = false "
                   "WHERE symbol = 'SPY'"))
    return _instrument(s, "SPY"), prev_active


def _bars(s, iid: str, pins: dict[int, str], *, n: int = len(SESSIONS),
          default: str = "100") -> None:
    """One vendor bar per session; close pinned by session index. Upsert:
    every instrument seeded here is private to this file, so a leftover bar
    from an interrupted committed run is ours to overwrite, never data."""
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, 1000, 'EodhdAdapter') "
        "ON CONFLICT (instrument_id, bar_date) DO UPDATE "
        "SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, "
        "    close = EXCLUDED.close, volume = EXCLUDED.volume, "
        "    source = EXCLUDED.source"),
        [{"iid": iid, "d": d, "c": pins.get(i, default)}
         for i, d in enumerate(SESSIONS[:n])])


def _memo(s, symbol: str, recommendation: str, *, at: datetime,
          shadow: bool | None = None, conviction: str | None = None,
          source: str | None = None) -> str:
    run_id = None
    if shadow is not None:
        run_id = s.execute(text(
            "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
            "model, status, shadow, created_at) "
            "VALUES ('committee', 'tmpl', 'test-model', 'ok', :sh, :ca) "
            "RETURNING id"), {"sh": shadow, "ca": at}).scalar()
    return str(s.execute(text(
        "INSERT INTO research.memos (agent_run_id, memo_type, instrument_symbol, "
        "recommendation, conviction, source, evidence_refs, created_at) "
        "VALUES (:r, 'committee', :sym, :rec, :conv, :src, '[]', :ca) "
        "RETURNING id"),
        {"r": run_id, "sym": symbol, "rec": recommendation, "conv": conviction,
         "src": source, "ca": at}).scalar())


def _seed_pinned(s) -> str:
    """ZSCO +10% @20s / +30% @60s off a 100 anchor; SPY +4% / -5% off 400.
    Hand-pinned expectations: excess 0.060000 @20 and 0.350000 @60."""
    zsco = _instrument(s, "ZSCO")
    spy, _ = _benchmark(s)
    _bars(s, zsco, {ANCHOR_IDX: "100", 25: "110", 65: "130"})
    _bars(s, spy, {ANCHOR_IDX: "400", 25: "416", 65: "380"}, default="400")
    return _memo(s, "ZSCO", "BUY", at=_at(SESSIONS[ANCHOR_IDX]))


def test_outcomes_written_with_hand_pinned_numbers(clean_audit):
    s = clean_audit
    memo_id = _seed_pinned(s)
    clock = FrozenClock(_at(SESSIONS[65], hour=22))

    rep = compute_memo_outcomes(s, clock)
    assert rep.summary() == "scorecard: +2 outcomes"

    rows = s.execute(text(
        "SELECT horizon_sessions, anchor_date, anchor_close, fwd_close, "
        " fwd_return, spy_return, excess, computed_at "
        "FROM research.memo_outcomes ORDER BY horizon_sessions")).all()
    assert [(r.horizon_sessions, r.anchor_date) for r in rows] == [
        (20, SESSIONS[ANCHOR_IDX]), (60, SESSIONS[ANCHOR_IDX])]
    h20, h60 = rows
    assert (h20.anchor_close, h20.fwd_close) == (Decimal("100"), Decimal("110"))
    assert h20.fwd_return == Decimal("0.100000")
    assert h20.spy_return == Decimal("0.040000")
    assert h20.excess == Decimal("0.060000")
    assert h60.fwd_return == Decimal("0.300000")
    assert h60.spy_return == Decimal("-0.050000")
    assert h60.excess == Decimal("0.350000")
    assert h20.computed_at == clock.now()               # injectable clock only

    evs = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'research.scorecard.updated'")).scalars().all()
    assert len(evs) == 1
    assert evs[0]["written"] == 2 and evs[0]["memo_ids"] == [memo_id]
    assert evs[0]["by_horizon"] == {"20": 1, "60": 1}

    # idempotent re-run: recorded facts stand, no rows, NO second audit event
    rep2 = compute_memo_outcomes(s, clock)
    assert rep2.written == () and rep2.already == 2
    assert rep2.summary() == "scorecard: none matured"
    assert s.execute(text(
        "SELECT count(*) FROM research.memo_outcomes")).scalar() == 2
    assert s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'research.scorecard.updated'")).scalar() == 1


def test_immature_memo_matures_only_as_the_clock_advances(clean_audit):
    """All 70 bars sit in the table from the start: maturation is decided by
    the injected clock's date (no look-ahead, CLAUDE.md invariant 8), never
    by what happens to be stored."""
    s = clean_audit
    _seed_pinned(s)

    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[15], hour=22)))
    assert rep.summary() == "scorecard: none matured" and rep.written == ()
    assert {sk.horizon_sessions for sk in rep.skipped} == {20, 60}
    assert all(sk.reason.startswith("immature") for sk in rep.skipped)
    assert s.execute(text(       # zero writes => zero audit noise
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'research.scorecard.updated'")).scalar() == 0

    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[25], hour=22)))
    assert [r.horizon_sessions for r in rep.written] == [20]
    assert [sk.horizon_sessions for sk in rep.skipped] == [60]

    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[65], hour=22)))
    assert [r.horizon_sessions for r in rep.written] == [60]
    assert rep.already == 1
    assert s.execute(text(
        "SELECT count(*) FROM research.memo_outcomes")).scalar() == 2


def test_ambiguous_symbol_fails_closed(clean_audit):
    """Two ACTIVE instruments sharing a symbol: the memo cannot be graded
    (same resolution rule as the bridge) — memo-level skip, nothing written."""
    s = clean_audit
    _instrument(s, "ZSCD", exchange="XTEST")
    _instrument(s, "ZSCD", exchange="XTES2")
    spy, _ = _benchmark(s)
    _bars(s, spy, {}, default="400")
    _memo(s, "ZSCD", "BUY", at=_at(SESSIONS[ANCHOR_IDX]))

    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[65], hour=22)))
    assert rep.written == ()
    assert len(rep.skipped) == 1
    assert rep.skipped[0].reason.startswith("no instrument")
    assert rep.skipped[0].horizon_sessions is None


# ----------------------- analysis-only resolution + top-up (desk-review item 5)

def test_inactive_single_row_symbol_grades_without_topup(clean_audit):
    """The resolver half of item 5: an analyze-box memo lands on an
    is_active=FALSE instrument (ops/analyze.py); when it is the symbol's only
    row and the bars are already stored, it grades end to end with NO adapter
    — grading needs bars, not tradability."""
    s = clean_audit
    zsci = _instrument(s, "ZSCI", active=False)
    spy, _ = _benchmark(s)
    _bars(s, zsci, {ANCHOR_IDX: "100", 25: "110"}, n=26)
    _bars(s, spy, {ANCHOR_IDX: "400", 25: "416"}, n=26, default="400")
    _memo(s, "ZSCI", "BUY", at=_at(SESSIONS[ANCHOR_IDX]), source="investing.com")

    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[25], hour=22)))
    assert [r.horizon_sessions for r in rep.written] == [20]
    assert rep.written[0].excess == Decimal("0.060000")


def test_active_row_beats_analysis_only_row(clean_audit):
    """One active + one inactive row for the symbol: the bridge's rule wins —
    the memo grades against the ACTIVE instrument's bars."""
    s = clean_audit
    act = _instrument(s, "ZSCJ")
    _instrument(s, "ZSCJ", exchange="XTES2", active=False)
    spy, _ = _benchmark(s)
    _bars(s, act, {ANCHOR_IDX: "100", 25: "120"}, n=26)   # only the ACTIVE has bars
    _bars(s, spy, {ANCHOR_IDX: "400", 25: "400"}, n=26, default="400")
    _memo(s, "ZSCJ", "BUY", at=_at(SESSIONS[ANCHOR_IDX]))

    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[25], hour=22)))
    assert rep.written[0].fwd_return == Decimal("0.200000")
    assert rep.written[0].excess == Decimal("0.200000")


def test_several_inactive_rows_still_fail_closed(clean_audit):
    """The relaxation is 'exactly one row of any activity', never 'pick one':
    two inactive rows stay ambiguous."""
    s = clean_audit
    _instrument(s, "ZSCK", active=False)
    _instrument(s, "ZSCK", exchange="XTES2", active=False)
    spy, _ = _benchmark(s)
    _bars(s, spy, {}, default="400")
    _memo(s, "ZSCK", "BUY", at=_at(SESSIONS[ANCHOR_IDX]))

    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[65], hour=22)))
    assert rep.written == ()
    assert len(rep.skipped) == 1
    assert rep.skipped[0].reason.startswith("no instrument")


def _fake_vendor(closes: dict[date, str], calls: list[tuple[date, date]]):
    """Deterministic in-memory vendor. The class is deliberately NAMED
    EodhdAdapter: the top-up stores bars under source=type(adapter).__name__
    (the house convention — analyze.py, daily ingest) and grading reads only
    vendor-source rows, so the fake impersonates the vendor adapter class the
    way a stubbed transport would. No network anywhere."""
    class EodhdAdapter:
        def fetch_splits(self, symbol, start, end):
            return []

        def fetch_bars(self, symbol, start, end):
            calls.append((start, end))
            return [Bar(symbol=symbol, bar_date=d, open=Decimal(c),
                        high=Decimal(c), low=Decimal(c), close=Decimal(c),
                        volume=1000)
                    for d, c in sorted(closes.items()) if start <= d <= end]
    return EodhdAdapter()


def test_analysis_only_memo_becomes_gradeable_via_topup(clean_audit):
    """Item 5 end to end: an analyze-style memo (inactive instrument, source
    tagged), bars stored only through the memo session — nightly ingest skips
    inactive instruments, so without the top-up its outcomes would never
    mature. The scorecard fetches exactly the missing window through the
    injected adapter factory, grades with hand-pinned numbers, and a re-run
    fetches nothing (already current through the last completed session)."""
    s = clean_audit
    zsct = _instrument(s, "ZSCT", active=False)
    spy, _ = _benchmark(s)
    _bars(s, zsct, {ANCHOR_IDX: "100"}, n=11)             # through SESSIONS[10] only
    _bars(s, spy, {ANCHOR_IDX: "400", 25: "416"}, n=26, default="400")
    memo_id = _memo(s, "ZSCT", "BUY", at=_at(SESSIONS[ANCHOR_IDX]),
                    conviction="HIGH", source="investing.com")

    closes = {d: "100" for d in SESSIONS[11:26]}          # the missing forward window
    closes[SESSIONS[25]] = "110"
    calls: list[tuple[date, date]] = []
    clock = FrozenClock(_at(SESSIONS[25], hour=22))

    rep = compute_memo_outcomes(
        s, clock, adapter_for=lambda sym, ex: _fake_vendor(closes, calls))
    assert [r.horizon_sessions for r in rep.written] == [20]
    assert rep.written[0].memo_id == memo_id
    assert rep.written[0].fwd_return == Decimal("0.100000")
    assert rep.written[0].spy_return == Decimal("0.040000")
    assert rep.written[0].excess == Decimal("0.060000")
    assert len(calls) == 1                                # one bounded fetch
    assert calls[0][1] <= clock.now().date()              # never a forward window
    assert rep.topups == (f"ZSCT: +15 bars through {SESSIONS[25]}",)
    assert "topped up 1 analysis-only symbol(s)" in rep.summary()

    # re-run at the same clock: 20s already recorded, 60s still awaited, but
    # the window is current — no fetch, no note (bounded means bounded)
    rep2 = compute_memo_outcomes(
        s, clock, adapter_for=lambda sym, ex: _fake_vendor(closes, calls))
    assert rep2.topups == () and len(calls) == 1
    assert rep2.already == 1


def test_topup_vendor_failure_is_failsoft_per_symbol(clean_audit):
    """A vendor failure never blocks grading: it is noted per symbol and the
    memo's horizons simply stay immature on the stored bars."""
    s = clean_audit
    zsct = _instrument(s, "ZSCT", active=False)
    spy, _ = _benchmark(s)
    _bars(s, zsct, {ANCHOR_IDX: "100"}, n=11)
    _bars(s, spy, {}, default="400")
    _memo(s, "ZSCT", "BUY", at=_at(SESSIONS[ANCHOR_IDX]))

    def broken(sym, ex):
        raise RuntimeError("vendor down")

    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[25], hour=22)),
                                adapter_for=broken)
    assert rep.written == ()
    assert rep.topups == ("ZSCT: top-up failed (vendor down)",)
    assert {sk.horizon_sessions for sk in rep.skipped} == {20, 60}
    assert all(sk.reason.startswith("immature") for sk in rep.skipped)


def test_topup_is_bounded_to_inactive_symbols_with_stored_bars(clean_audit):
    """Bounded means: active instruments are t0 ingest's job, and an inactive
    instrument with NO stored vendor bars is a backfill, not maintenance —
    neither reaches the vendor factory at all."""
    s = clean_audit
    active = _instrument(s, "ZSCU")                       # active, stale bars
    _instrument(s, "ZSCV", active=False)                  # inactive, zero bars
    spy, _ = _benchmark(s)
    _bars(s, active, {ANCHOR_IDX: "100"}, n=11)
    _bars(s, spy, {}, default="400")
    _memo(s, "ZSCU", "BUY", at=_at(SESSIONS[ANCHOR_IDX]))
    _memo(s, "ZSCV", "BUY", at=_at(SESSIONS[ANCHOR_IDX]))

    asked: list[str] = []

    def factory(sym, ex):
        asked.append(sym)
        return _fake_vendor({}, [])

    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[25], hour=22)),
                                adapter_for=factory)
    assert asked == []
    assert rep.topups == ()


# ------------------------------------------------------------------ API shape

@pytest.fixture
def client(monkeypatch, clean_audit):
    """TestClient reads over its own connection, so the scorecard data is
    COMMITTED here and cleaned up explicitly at teardown (read-surface
    pattern). Only the 20-session horizon matures: bars stop at index 25."""
    from fastapi.testclient import TestClient

    from atlas.api.main import app

    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = clean_audit
    zsca = _instrument(s, "ZSCA")
    zscb = _instrument(s, "ZSCB")
    zscc = _instrument(s, "ZSCC")
    zsce = _instrument(s, "ZSCE")
    spy, prev_active_spys = _benchmark(s)
    n = 26                                               # h20 matures, h60 never
    _bars(s, zsca, {ANCHOR_IDX: "100", 25: "110"}, n=n)  # +10% -> excess +6%
    _bars(s, zscb, {ANCHOR_IDX: "100", 25: "98"}, n=n)   # -2%  -> excess -6%
    _bars(s, zscc, {ANCHOR_IDX: "100", 25: "104"}, n=n)  # +4%  -> excess 0
    _bars(s, zsce, {ANCHOR_IDX: "100", 25: "98"}, n=n)   # -2%  -> excess -6%
    _bars(s, spy, {ANCHOR_IDX: "400", 25: "416"}, n=n, default="400")  # +4%
    at = _at(SESSIONS[ANCHOR_IDX])
    # conviction/source chosen to pin every slice kind: HIGH/LOW within BUY,
    # NULL source ('desk nightly') vs 'investing.com' (the pick-filter loop)
    ids = {"BUY": _memo(s, "ZSCA", "BUY", at=at, conviction="HIGH"),
           "REJECT": _memo(s, "ZSCB", "REJECT", at=at, conviction="MEDIUM",
                           source="investing.com"),
           "HOLD": _memo(s, "ZSCC", "HOLD", at=at, conviction="LOW"),
           "SHADOW": _memo(s, "ZSCA", "BUY", at=at, shadow=True,
                           conviction="HIGH"),
           "BUY_LOW": _memo(s, "ZSCE", "BUY", at=at, conviction="LOW",
                            source="investing.com")}
    rep = compute_memo_outcomes(s, FrozenClock(_at(SESSIONS[25], hour=22)))
    assert len(rep.written) == 5                         # one h20 row per memo
    s.commit()
    yield TestClient(app), s, ids
    # committed seeds -> explicit teardown. Pre-existing SPY rows belong to
    # other suites: their bars were never touched, and their is_active flags
    # are restored verbatim; the private XTEST SPY leaves with its bars.
    s.execute(text("TRUNCATE audit.decision_events, research.memos, "
                   "research.agent_runs RESTART IDENTITY CASCADE"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol LIKE 'ZSC%')"))
    s.execute(text("DELETE FROM market.price_bars_daily "
                   "WHERE instrument_id = :spy"), {"spy": spy})
    s.execute(text("DELETE FROM market.instruments WHERE symbol LIKE 'ZSC%'"))
    s.execute(text("DELETE FROM market.instruments WHERE id = :spy"),
              {"spy": spy})
    for iid in prev_active_spys:
        s.execute(text("UPDATE market.instruments SET is_active = true "
                       "WHERE id = :iid"), {"iid": iid})
    s.commit()
    reset_app_engine()


def test_scorecard_api_shape_pinned(client):
    """Hand-pinned end to end. The h20 dart universe is FIVE tracked rows —
    +6% (BUY), -6% (REJECT), 0% (HOLD), +6% (shadow), -6% (losing BUY) — so
    the direction-blind dart scores 2/5 = 0.4 in EITHER direction (the dead
    heat counts for neither). BUY: 1/2 vindicated, edge 0.5-0.4 = +0.1;
    REJECT: 1/1, edge 1.0-0.4 = +0.6."""
    c, _, ids = client
    r = c.get("/v1/research/scorecard")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"by_recommendation", "recent", "shadow_excluded",
                      "dart_tracked"}
    assert set(d["by_recommendation"]) == {"BUY", "REJECT"}
    assert d["dart_tracked"] == {"20": 5, "60": 0}

    buy, rej = d["by_recommendation"]["BUY"], d["by_recommendation"]["REJECT"]
    expected_keys = {"memos", "by_conviction", "by_source"} | {
        f"{k}_{h}" for h in (20, 60)
        for k in ("matured", "vindicated", "avg_excess", "dissent_right",
                  "baseline", "rate_minus_baseline")}
    assert set(buy) == expected_keys and set(rej) == expected_keys

    # the shadow BUY memo is EXCLUDED from the rates (non-actionable)
    assert buy == {
        "memos": 2, "matured_20": 2, "vindicated_20": 1,
        "avg_excess_20": pytest.approx(0.0),
        "dissent_right_20": 1,                     # the losing BUY's dissent
        "baseline_20": pytest.approx(0.4),
        "rate_minus_baseline_20": pytest.approx(0.1),
        "matured_60": 0, "vindicated_60": 0, "avg_excess_60": None,
        "dissent_right_60": 0, "baseline_60": None,
        "rate_minus_baseline_60": None,
        "by_conviction": buy["by_conviction"], "by_source": buy["by_source"]}
    # REJECT vindicated: the desk dodged an underperformer (excess -6%) —
    # but the honest number is the +60pp edge over the dart, not the 1/1
    assert rej == {
        "memos": 1, "matured_20": 1, "vindicated_20": 1,
        "avg_excess_20": pytest.approx(-0.06),
        "dissent_right_20": 0,
        "baseline_20": pytest.approx(0.4),
        "rate_minus_baseline_20": pytest.approx(0.6),
        "matured_60": 0, "vindicated_60": 0, "avg_excess_60": None,
        "dissent_right_60": 0, "baseline_60": None,
        "rate_minus_baseline_60": None,
        "by_conviction": rej["by_conviction"], "by_source": rej["by_source"]}
    assert d["shadow_excluded"] == 1

    # conviction slices, canonical order, dissent as the exact complement
    slice_keys = {"memos"} | {f"{k}_{h}" for h in (20, 60)
                              for k in ("matured", "vindicated", "dissent_right")}
    assert list(buy["by_conviction"]) == ["HIGH", "LOW"]
    high, low = buy["by_conviction"]["HIGH"], buy["by_conviction"]["LOW"]
    assert set(high) == slice_keys
    assert (high["memos"], high["matured_20"], high["vindicated_20"],
            high["dissent_right_20"]) == (1, 1, 1, 0)
    assert (low["memos"], low["matured_20"], low["vindicated_20"],
            low["dissent_right_20"]) == (1, 1, 0, 1)
    assert rej["by_conviction"]["MEDIUM"]["vindicated_20"] == 1

    # source slices: NULL = 'desk nightly' first, external tags verbatim —
    # the Principal's pick-filter loop verdict reads straight off this
    assert list(buy["by_source"]) == ["desk nightly", "investing.com"]
    assert (buy["by_source"]["desk nightly"]["vindicated_20"],
            buy["by_source"]["desk nightly"]["matured_20"]) == (1, 1)
    assert (buy["by_source"]["investing.com"]["vindicated_20"],
            buy["by_source"]["investing.com"]["dissent_right_20"]) == (0, 1)
    assert list(rej["by_source"]) == ["investing.com"]

    assert len(d["recent"]) == 5                        # HOLD + shadow visible
    row = d["recent"][0]
    assert set(row) == {"memo_id", "symbol", "recommendation", "conviction",
                        "source", "horizon", "fwd_return", "spy_return",
                        "excess", "vindicated", "dissent_right"}
    by_memo = {x["memo_id"]: x for x in d["recent"]}
    assert by_memo[ids["BUY"]]["vindicated"] is True
    assert by_memo[ids["BUY"]]["dissent_right"] is False
    assert by_memo[ids["BUY"]]["excess"] == pytest.approx(0.06)
    assert by_memo[ids["BUY"]]["conviction"] == "HIGH"
    assert by_memo[ids["BUY"]]["source"] is None        # the desk's own work
    assert by_memo[ids["REJECT"]]["vindicated"] is True
    assert by_memo[ids["REJECT"]]["dissent_right"] is False
    assert by_memo[ids["REJECT"]]["source"] == "investing.com"
    assert by_memo[ids["HOLD"]]["vindicated"] is None   # no direction to grade
    assert by_memo[ids["HOLD"]]["dissent_right"] is None
    assert by_memo[ids["SHADOW"]]["vindicated"] is None  # non-actionable
    assert by_memo[ids["SHADOW"]]["dissent_right"] is None
    assert by_memo[ids["BUY_LOW"]]["vindicated"] is False
    assert by_memo[ids["BUY_LOW"]]["dissent_right"] is True
    assert all(x["horizon"] == 20 for x in d["recent"])


def test_memos_endpoint_carries_the_outcome_badge(client):
    c, _, ids = client
    memos = {m["id"]: m for m in c.get("/v1/research/memos").json()}
    buy = memos[ids["BUY"]]
    assert buy["outcome_20"]["excess"] == pytest.approx(0.06)
    assert buy["outcome_20"]["fwd_return"] == pytest.approx(0.10)
    assert buy["outcome_20"]["spy_return"] == pytest.approx(0.04)
    assert buy["outcome_20"]["vindicated"] is True
    assert memos[ids["REJECT"]]["outcome_20"]["vindicated"] is True
    assert memos[ids["HOLD"]]["outcome_20"]["vindicated"] is None
    assert memos[ids["SHADOW"]]["outcome_20"]["vindicated"] is None


# ------------------------------------------------------------------ T9 wiring

def test_t9_report_carries_the_scorecard_line(clean_audit):
    """Sunday cycle on an empty book (test_daily_cycle_pg pattern): the T9
    summary always carries the scorecard fragment."""
    s = clean_audit
    s.execute(text("DELETE FROM workflow.workflow_node_results "
                   "WHERE run_id = 'daily-2026-07-12'"))
    s.execute(text("DELETE FROM workflow.workflow_runs "
                   "WHERE run_id = 'daily-2026-07-12'"))
    clock = FrozenClock(datetime(2026, 7, 12, 23, 30, tzinfo=UTC))
    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert "scorecard: none matured" in results["t9_report"]


def test_scorecard_failure_is_fail_soft_and_pages(clean_audit, monkeypatch):
    """A scorecard crash never blocks the report: the line records it and the
    operator is paged at high priority (desk_failed-style), but T9 completes
    and the daily_cycle.completed event still lands."""
    s = clean_audit
    s.execute(text("DELETE FROM workflow.workflow_node_results "
                   "WHERE run_id = 'daily-2026-07-19'"))
    s.execute(text("DELETE FROM workflow.workflow_runs "
                   "WHERE run_id = 'daily-2026-07-19'"))

    def boom(session, clock, adapter_for=None):
        raise RuntimeError("scorecard melted")

    pages: list[tuple[str, str, str]] = []
    monkeypatch.setattr("atlas.ops.daily.compute_memo_outcomes", boom)
    monkeypatch.setattr(
        "atlas.ops.daily.notify",
        lambda title, message, *, priority="default":
            pages.append((title, message, priority)) or True)

    clock = FrozenClock(datetime(2026, 7, 19, 23, 30, tzinfo=UTC))
    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert "scorecard FAILED: scorecard melted" in results["t9_report"]
    assert pages and pages[-1][2] == "high"
    assert s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'daily_cycle.completed'")).scalar() == 1


def test_t9_passes_the_topup_adapter_factory(clean_audit, monkeypatch):
    """The one surgical daily.py addition (desk-review item 5): t9 hands
    compute_memo_outcomes the vendor adapter factory, so the nightly cycle
    covers analysis-only bar maintenance without touching anything else.
    (Active instruments never reach the factory — see the bounded test —
    so cycles without inactive awaiting memos never construct an adapter.)"""
    from atlas.dcp.scorecard import ScorecardReport, vendor_adapter_for

    s = clean_audit
    s.execute(text("DELETE FROM workflow.workflow_node_results "
                   "WHERE run_id = 'daily-2026-07-26'"))
    s.execute(text("DELETE FROM workflow.workflow_runs "
                   "WHERE run_id = 'daily-2026-07-26'"))
    seen: dict[str, object] = {}

    def spy_compute(session, clock, adapter_for=None):
        seen["adapter_for"] = adapter_for
        return ScorecardReport(written=(), skipped=(), already=0)

    monkeypatch.setattr("atlas.ops.daily.compute_memo_outcomes", spy_compute)
    clock = FrozenClock(datetime(2026, 7, 26, 23, 30, tzinfo=UTC))
    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert seen["adapter_for"] is vendor_adapter_for
    assert "scorecard: none matured" in results["t9_report"]
