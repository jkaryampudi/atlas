"""Learning loop v1 against the real database (migration 0030): matured
scorecard outcomes labeled with hand-pinned values, specialist seats graded
from real research.memo_specialists rows, lessons templated, the calibration
snapshot pinned through the EXISTING calibration.py math, append-only
prev_weight lineage across two nights, T9 cycle wiring (fail-soft, pages like
a desk failure), the /v1/learning/summary API shape, and the honest empty
state. The pure mapping/template/formula goldens live in
tests/unit/test_learning_*.py; this file proves the plumbing end to end.

Seeding follows the house pattern (test_scorecard_pg.py): everything inside
the test transaction (rolled back by pg_session) except the API tests, which
must commit for the TestClient's own connection and clean up explicitly at
teardown. learning.* tables have NO FK into research.memos (0002), so
clean_audit's CASCADE never clears them — every test starts by truncating
them inside its own transaction (or commits and restores, for the API).

HAND-PINNED FIXTURE (mirrors the unit goldens): ZSCL BUY HIGH, anchor 100;
SPY anchor 400. h20: ZSCL 98 (-2%), SPY 416 (+4%) -> excess -0.060000, the
BUY FAILED. h60: ZSCL 130 (+30%), SPY 380 (-5%) -> excess +0.350000,
VINDICATED. Panel: quality concerned/high with 2 red flags, growth
supportive/low with none, macro neutral/medium with 1.

Calibration hand math (ADR-0003 constants: baseline 0.25, gain 4, K=30):
night 1 (h20 only)  conviction:HIGH n=1 miss  brier 0.5625
                    weight = 1 - 1.25/31  = 0.95967741...
night 2 (h20+h60)   conviction:HIGH n=2 (miss, hit) brier 0.3125
                    weight = 1 - 0.25*(2/32) = 0.984375, prev = night 1
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.learning.loop import run_learning
from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
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


SESSIONS = _weekdays(70)
ANCHOR_IDX = 5


def _at(session_date: date, hour: int = 21) -> datetime:
    return datetime.combine(session_date, time(hour, 0), tzinfo=UTC)


def _clean_learning(s) -> None:
    s.execute(text("TRUNCATE learning.outcome_labels, learning.lessons, "
                   "learning.agent_calibration"))


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
    """Park pre-existing SPY rows inactive; grade against a private XTEST SPY
    (the scorecard-suite pattern — see test_scorecard_pg._benchmark)."""
    prev_active = [str(r.id) for r in s.execute(text(
        "SELECT id FROM market.instruments "
        "WHERE symbol = 'SPY' AND is_active")).all()]
    s.execute(text("UPDATE market.instruments SET is_active = false "
                   "WHERE symbol = 'SPY'"))
    return _instrument(s, "SPY"), prev_active


def _bars(s, iid: str, pins: dict[int, str], *, n: int = len(SESSIONS),
          default: str = "100") -> None:
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


PANEL = {
    "quality": {"stance": "concerned", "confidence": "high",
                "key_points": ["a", "b"], "red_flags": ["r1", "r2"]},
    "growth": {"stance": "supportive", "confidence": "low",
               "key_points": ["a", "b"], "red_flags": []},
    "macro": {"stance": "neutral", "confidence": "medium",
              "key_points": ["a", "b"], "red_flags": ["r3"]},
}


def _specialists(s, memo_id: str, panel: dict = PANEL) -> None:
    s.execute(text(
        "INSERT INTO research.memo_specialists (memo_id, role, payload) "
        "VALUES (:m, :role, CAST(:p AS jsonb))"),
        [{"m": memo_id, "role": role, "p": json.dumps(payload)}
         for role, payload in panel.items()])


def _seed_failed_buy(s) -> str:
    """The module-docstring fixture: HIGH BUY that fails at 20s (-6% excess)
    and is vindicated at 60s (+35%), with the three-seat panel."""
    zscl = _instrument(s, "ZSCL")
    spy, _ = _benchmark(s)
    _bars(s, zscl, {ANCHOR_IDX: "100", 25: "98", 65: "130"})
    _bars(s, spy, {ANCHOR_IDX: "400", 25: "416", 65: "380"}, default="400")
    memo_id = _memo(s, "ZSCL", "BUY", at=_at(SESSIONS[ANCHOR_IDX]),
                    conviction="HIGH")
    _specialists(s, memo_id)
    return memo_id


W_HIGH_N1 = 1 - 1.25 / 31              # night-1 conviction:HIGH weight
W_HIGH_N2 = 1 - 0.25 * (2 / 32)        # night-2, after the 60s hit


def test_labels_lessons_and_audit_end_to_end(clean_audit):
    s = clean_audit
    _clean_learning(s)
    memo_id = _seed_failed_buy(s)
    clock = FrozenClock(_at(SESSIONS[25], hour=22))

    assert len(compute_memo_outcomes(s, clock).written) == 1     # h20 matured
    rep = run_learning(s, clock)
    assert rep.labeling.summary() == ("learning: +1 outcome labels "
                                      "(+3 specialist), +3 lessons")

    ml = s.execute(text(
        "SELECT * FROM learning.outcome_labels WHERE label_kind = 'memo'")
        ).mappings().one()
    assert str(ml["thesis_memo_id"]) == memo_id
    assert ml["horizon_sessions"] == 20
    assert ml["recommendation"] == "BUY" and ml["conviction"] == "HIGH"
    assert ml["source"] is None and ml["shadow"] is False
    assert ml["direction_vindicated"] is False
    assert ml["excess"] == Decimal("-0.060000")
    assert ml["labeled_at"] == clock.now()               # injectable clock only

    spec = {r["specialist_role"]: r for r in s.execute(text(
        "SELECT * FROM learning.outcome_labels "
        "WHERE label_kind = 'specialist'")).mappings()}
    assert set(spec) == {"quality", "growth", "macro"}
    q, g, m = spec["quality"], spec["growth"], spec["macro"]
    assert (q["specialist_stance"], q["specialist_confidence"],
            q["n_red_flags"]) == ("concerned", "high", 2)
    assert q["aligned"] is True and q["flag_validated"] is True
    assert g["aligned"] is False and g["flag_validated"] is None
    assert m["aligned"] is None and m["flag_validated"] is True

    lessons = s.execute(text(
        "SELECT source_type, source_id, lesson, tags FROM learning.lessons "
        "ORDER BY lesson")).all()
    assert [tuple(r.tags) for r in lessons] == [
        ("high_conviction_call_failed", "h20", "BUY"),
        ("specialist_flags_validated", "h20", "BUY", "macro"),
        ("specialist_flags_validated", "h20", "BUY", "quality")]
    assert all(r.source_type == "memo_outcome"
               and str(r.source_id) == memo_id for r in lessons)
    assert lessons[0].lesson == (
        "HIGH-conviction BUY on ZSCL was not vindicated at 20 sessions: "
        "excess -6.00% vs SPY — the dissent was right.")
    assert lessons[2].lesson == (
        "quality specialist flagged 2 risk(s) on ZSCL before the BUY; the "
        "call failed at 20 sessions (excess -6.00% vs SPY) — flags validated.")

    evs = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'learning.outcomes.labeled'")).scalars().all()
    assert len(evs) == 1
    assert evs[0]["memo_labels"] == 1 and evs[0]["specialist_labels"] == 3
    assert evs[0]["lessons"] == 3 and evs[0]["memo_ids"] == [memo_id]
    assert evs[0]["by_horizon"] == {"20": 1, "60": 0}

    # idempotent re-run: recorded labels stand, no rows, NO second event
    rep2 = run_learning(s, clock)
    assert rep2.labeling.memo_labels == ()
    assert rep2.labeling.specialist_labels == ()
    assert rep2.labeling.already == 1
    assert rep2.calibration is None
    assert rep2.summary() == "learning: nothing newly matured"
    assert s.execute(text(
        "SELECT count(*) FROM learning.outcome_labels")).scalar() == 4
    assert s.execute(text(
        "SELECT count(*) FROM learning.lessons")).scalar() == 3
    assert s.execute(text(
        "SELECT count(*) FROM audit.decision_events WHERE event_type "
        "LIKE 'learning.%'")).scalar() == 2      # labeled + snapshot, once each


def test_calibration_snapshot_pinned_with_prev_weight_lineage(clean_audit):
    s = clean_audit
    _clean_learning(s)
    _seed_failed_buy(s)

    # night 1: only the 20s outcome exists — the HIGH miss
    clock1 = FrozenClock(_at(SESSIONS[25], hour=22))
    compute_memo_outcomes(s, clock1)
    rep1 = run_learning(s, clock1)
    assert rep1.calibration is not None
    period1 = clock1.now().astimezone(UTC).date().isoformat()
    assert rep1.calibration.period == period1

    rows = {r.agent_role: r for r in s.execute(text(
        "SELECT * FROM learning.agent_calibration WHERE period = :p"),
        {"p": period1}).all()}
    assert set(rows) == {"conviction:HIGH", "specialist:quality",
                         "specialist:growth", "source:desk nightly"}
    high = rows["conviction:HIGH"]
    assert high.n_forecasts == 1 and high.regime == "all"
    assert float(high.brier_score) == pytest.approx(0.5625)
    assert float(high.conviction_weight) == pytest.approx(W_HIGH_N1)
    assert high.prev_weight is None                     # first snapshot ever
    assert high.updated_at == clock1.now()
    # macro seat was neutral (no claim) — no calibration row for it
    assert float(rows["specialist:quality"].conviction_weight) == \
        pytest.approx(1 + 0.75 / 31)
    assert float(rows["specialist:growth"].conviction_weight) == \
        pytest.approx(1 - 0.21 / 31)

    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'learning.calibration.snapshot'")).scalars().one()
    assert ev["applied"] is False
    assert ev["rows"] == 4 and ev["period"] == period1
    assert ev["weights"]["conviction:HIGH"]["n"] == 1
    assert ev["specialist_reliability"]["quality"]["alignment_rate"] == 1.0
    assert ev["specialist_reliability"]["quality"]["flag_validation_rate"] == 1.0
    # the failed BUY is the whole h20 universe: dart baseline 0/1, edge 0
    assert ev["source_trust"]["desk nightly"]["20"] == {
        "rate": 0.0, "baseline": 0.0, "edge": 0.0, "n": 1}

    # night 2: the 60s outcome matures a vindication; prev_weight = night 1
    clock2 = FrozenClock(_at(SESSIONS[65], hour=22))
    compute_memo_outcomes(s, clock2)
    rep2 = run_learning(s, clock2)
    assert rep2.calibration is not None
    period2 = clock2.now().astimezone(UTC).date().isoformat()
    high2 = s.execute(text(
        "SELECT * FROM learning.agent_calibration "
        "WHERE period = :p AND agent_role = 'conviction:HIGH'"),
        {"p": period2}).one()
    assert high2.n_forecasts == 2
    assert float(high2.brier_score) == pytest.approx(0.3125)
    assert float(high2.conviction_weight) == pytest.approx(W_HIGH_N2)
    assert float(high2.prev_weight) == pytest.approx(W_HIGH_N1)
    # night 1's snapshot is untouched — append-only, a fact once taken
    assert float(s.execute(text(
        "SELECT conviction_weight FROM learning.agent_calibration "
        "WHERE period = :p AND agent_role = 'conviction:HIGH'"),
        {"p": period1}).scalar()) == pytest.approx(W_HIGH_N1)
    # no new lessons on a vindication night
    assert s.execute(text(
        "SELECT count(*) FROM learning.lessons")).scalar() == 3


def test_shadow_memo_labels_without_specialist_grades(clean_audit):
    """A shadow memo's outcome is labeled (record complete, vindicated NULL,
    shadow TRUE) but its panel is never graded and no calibration forecast
    uses it."""
    s = clean_audit
    _clean_learning(s)
    zscm = _instrument(s, "ZSCM")
    spy, _ = _benchmark(s)
    _bars(s, zscm, {ANCHOR_IDX: "100", 25: "110"}, n=26)
    _bars(s, spy, {ANCHOR_IDX: "400", 25: "416"}, n=26, default="400")
    memo_id = _memo(s, "ZSCM", "BUY", at=_at(SESSIONS[ANCHOR_IDX]),
                    shadow=True, conviction="HIGH")
    _specialists(s, memo_id)
    clock = FrozenClock(_at(SESSIONS[25], hour=22))
    compute_memo_outcomes(s, clock)

    rep = run_learning(s, clock)
    assert len(rep.labeling.memo_labels) == 1
    assert rep.labeling.specialist_labels == ()
    assert rep.labeling.lessons == ()
    row = s.execute(text(
        "SELECT shadow, direction_vindicated FROM learning.outcome_labels")
        ).one()
    assert row.shadow is True and row.direction_vindicated is None
    # a shadow-only corpus has no scoreable forecasts: no snapshot rows
    assert rep.calibration is not None and rep.calibration.rows == ()
    assert s.execute(text(
        "SELECT count(*) FROM learning.agent_calibration")).scalar() == 0


# ------------------------------------------------------------------ T9 wiring

def test_t9_report_carries_the_learning_line(clean_audit):
    """Sunday cycle on an empty book (test_daily_cycle_pg pattern): the T9
    summary always carries the learning fragment, honest no-op included."""
    s = clean_audit
    _clean_learning(s)
    s.execute(text("DELETE FROM workflow.workflow_node_results "
                   "WHERE run_id = 'daily-2026-08-09'"))
    s.execute(text("DELETE FROM workflow.workflow_runs "
                   "WHERE run_id = 'daily-2026-08-09'"))
    clock = FrozenClock(datetime(2026, 8, 9, 23, 30, tzinfo=UTC))
    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert "learning: nothing newly matured" in results["t9_report"]


def test_learning_failure_is_fail_soft_and_pages(clean_audit, monkeypatch):
    """A learning crash never blocks the report: the line records it and the
    operator is paged at high priority (scorecard-failure pattern), but T9
    completes and the daily_cycle.completed event still lands."""
    s = clean_audit
    _clean_learning(s)
    s.execute(text("DELETE FROM workflow.workflow_node_results "
                   "WHERE run_id = 'daily-2026-08-16'"))
    s.execute(text("DELETE FROM workflow.workflow_runs "
                   "WHERE run_id = 'daily-2026-08-16'"))

    def boom(session, clock):
        raise RuntimeError("learning melted")

    pages: list[tuple[str, str, str]] = []
    monkeypatch.setattr("atlas.ops.daily.run_learning", boom)
    monkeypatch.setattr(
        "atlas.ops.daily.notify",
        lambda title, message, *, priority="default":
            pages.append((title, message, priority)) or True)

    clock = FrozenClock(datetime(2026, 8, 16, 23, 30, tzinfo=UTC))
    results = run_daily_cycle(s, clock, FixtureAdapter(FIXTURES))
    assert "learning FAILED: learning melted" in results["t9_report"]
    assert "learning FAILED — see log" in results["t9_report"]
    assert pages and pages[-1][2] == "high"
    assert s.execute(text(
        "SELECT count(*) FROM audit.decision_events "
        "WHERE event_type = 'daily_cycle.completed'")).scalar() == 1


# ------------------------------------------------------------------ API shape

@pytest.fixture
def client(monkeypatch, clean_audit):
    """TestClient reads over its own connection: the learning corpus is
    COMMITTED here and cleaned up explicitly at teardown (the scorecard-suite
    pattern)."""
    from fastapi.testclient import TestClient

    from atlas.api.main import app

    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = clean_audit
    _clean_learning(s)
    prev_active_spys = [str(r.id) for r in s.execute(text(
        "SELECT id FROM market.instruments "
        "WHERE symbol = 'SPY' AND is_active")).all()]   # before parking them
    memo_id = _seed_failed_buy(s)
    spy = s.execute(text("SELECT id FROM market.instruments "
                         "WHERE symbol = 'SPY' AND exchange = 'XTEST'")).scalar()
    clock = FrozenClock(_at(SESSIONS[25], hour=22))
    compute_memo_outcomes(s, clock)
    run_learning(s, clock)
    s.commit()
    yield TestClient(app), s, memo_id, clock
    _clean_learning(s)
    s.execute(text("TRUNCATE audit.decision_events, research.memos, "
                   "research.agent_runs RESTART IDENTITY CASCADE"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol IN "
                   "('ZSCL', 'ZSCM'))"))
    s.execute(text("DELETE FROM market.price_bars_daily "
                   "WHERE instrument_id = :spy"), {"spy": spy})
    s.execute(text("DELETE FROM market.instruments WHERE symbol IN "
                   "('ZSCL', 'ZSCM')"))
    s.execute(text("DELETE FROM market.instruments WHERE id = :spy"),
              {"spy": spy})
    for iid in prev_active_spys:
        s.execute(text("UPDATE market.instruments SET is_active = true "
                       "WHERE id = :iid"), {"iid": iid})
    s.commit()
    reset_app_engine()


def test_learning_summary_api_shape_pinned(client):
    c, _, memo_id, clock = client
    r = c.get("/v1/learning/summary")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"labels", "calibration", "specialists", "sources",
                      "lessons", "applied", "note"}
    assert d["applied"] is False
    assert "never applied" in d["note"]

    assert d["labels"] == {"memo": 1, "specialist": 3,
                           "graded_directional": 1,
                           "by_horizon": {"20": 1, "60": 0}}

    period = clock.now().astimezone(UTC).date().isoformat()
    assert d["calibration"]["as_of"] == period
    high = d["calibration"]["by_conviction"]["HIGH"]
    assert high["n"] == 1
    assert high["brier"] == pytest.approx(0.5625)
    assert high["weight"] == pytest.approx(W_HIGH_N1)
    assert high["prev_weight"] is None

    q = d["specialists"]["quality"]
    assert q["alignment_rate"] == pytest.approx(1.0)
    assert (q["n_graded"], q["n_flagged"]) == (1, 1)
    assert q["flag_validation_rate"] == pytest.approx(1.0)
    assert q["weight"] == pytest.approx(1 + 0.75 / 31)
    m = d["specialists"]["macro"]                     # neutral: rates only
    assert m["alignment_rate"] is None and m["n_graded"] == 0
    assert m["flag_validation_rate"] == pytest.approx(1.0)
    assert "weight" not in m                          # no claim, no weight row

    desk = d["sources"]["desk nightly"]
    assert desk["h20"] == {"rate": 0.0, "baseline": 0.0, "edge": 0.0,
                           "n_graded": 1, "n_vindicated": 0}
    assert "h60" not in desk
    assert desk["weight"] == pytest.approx(W_HIGH_N1)

    assert d["lessons"]["count"] == 3
    assert len(d["lessons"]["recent"]) == 3
    texts = {x["lesson"] for x in d["lessons"]["recent"]}
    assert ("HIGH-conviction BUY on ZSCL was not vindicated at 20 sessions: "
            "excess -6.00% vs SPY — the dissent was right.") in texts
    assert all(x["tags"][0] in ("high_conviction_call_failed",
                                "specialist_flags_validated")
               for x in d["lessons"]["recent"])


@pytest.fixture
def empty_client(monkeypatch, clean_audit):
    """No matured outcomes, no labels, no snapshot: the API must answer with
    an honest empty structure, never an error and never a fabricated row."""
    from fastapi.testclient import TestClient

    from atlas.api.main import app

    monkeypatch.setenv("ATLAS_DATABASE_URL", URL)
    reset_app_engine()
    s = clean_audit
    _clean_learning(s)
    s.commit()
    yield TestClient(app)
    reset_app_engine()


def test_learning_summary_empty_state(empty_client):
    d = empty_client.get("/v1/learning/summary").json()
    assert d["labels"] == {"memo": 0, "specialist": 0,
                           "graded_directional": 0,
                           "by_horizon": {"20": 0, "60": 0}}
    assert d["calibration"] == {"as_of": None, "by_conviction": {}}
    assert d["specialists"] == {} and d["sources"] == {}
    assert d["lessons"] == {"count": 0, "recent": []}
    assert d["applied"] is False
