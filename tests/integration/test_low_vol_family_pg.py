"""The low-vol family (catalog widening): golden pins for the vol math (split
adjustment, gap/nonpositive fail-closed), the ANCHOR — the compute at
window=20 byte-identical to the production risk panel's vol_20d_ann — the
catalog/lineage binding, an end-to-end recipe trial ranking low_vol_252 on
the fixture world (runner evidence for the widened catalog), and the repin
tool's refusal discipline. Committed registry rows are scrubbed by family
(the recipe-test hygiene convention); everything else stays in the rolled-
back test transaction.
"""
from __future__ import annotations

import math
import statistics
from decimal import Decimal
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.factory.families.low_vol import LOW_VOL_252
from atlas.dcp.factory.features import FEATURE_LINEAGE, RANKABLE_FEATURES
from atlas.dcp.factory.recipe_run import run_recipe
from atlas.dcp.factory.spec import RecipeSpec
from atlas.dcp.features.store import register_feature
from atlas.dcp.features.volatility import make_vol_compute
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.research.stock_models import compute_models
from atlas.tools.repin_features import RepinRefused, repin_feature
from tests.conftest import URL, requires_pg
from tests.integration.test_impl_variant_pg import _seed as _seed_ziv_world

pytestmark = requires_pg

_CLOCK = FrozenClock(datetime(2026, 7, 20, 6, 0, tzinfo=UTC))


def _scrub_recipe_family(prefix: str) -> None:
    engine = create_engine(URL)
    try:
        with engine.begin() as c:
            c.execute(text("DELETE FROM quant.trial_registry "
                           "WHERE strategy_family LIKE :f"), {"f": prefix + "%"})
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _registry_isolation(pg_session):
    _scrub_recipe_family("recipe-lowvol-ziv")
    yield
    _scrub_recipe_family("recipe-lowvol-ziv")


def _instrument(s, sym: str):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        " instrument_type, name, currency, is_active) "
        "VALUES (:s,'US','US','stock',:s,'USD',true) RETURNING id"),
        {"s": sym}).scalar()


def _bars_on_sessions(s, iid, sessions: list[date], closes: list[float]) -> None:
    """Bars EXACTLY on US sessions (the compute's contiguity rule and the
    production risk panel's return set must see the identical dates)."""
    assert len(sessions) == len(closes)
    for d, c in zip(sessions, closes):
        s.execute(text(
            "INSERT INTO market.price_bars_daily (instrument_id, bar_date, "
            " open, high, low, close, volume, source) "
            "VALUES (:i, :d, :c, :c, :c, :c, 1000, 'EodhdAdapter')"),
            # Decimal(str(c)) — the ingest/store bind discipline: a raw float
            # bind would be truncated by Postgres' float8->numeric cast to 15
            # significant digits, silently breaking byte-identity assertions
            {"i": iid, "d": d, "c": Decimal(str(c))})


def _split(s, iid, on: date, ratio: str) -> None:
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_type, "
        " action_date, ratio, source) VALUES (:i,'split',:d,:r,'EodhdAdapter')"),
        {"i": iid, "d": on, "r": ratio})


def _sessions_ending(end: date, n: int) -> list[date]:
    days = trading_days_between("US", end - timedelta(days=int(n * 2.2) + 40), end)
    assert len(days) >= n
    return days[-n:]


# ------------------------------------------------------- golden pins --------

def test_vol_golden_hand_computed(pg_session):
    s = pg_session
    iid = _instrument(s, "VOLA")
    sessions = _sessions_ending(date(2026, 7, 17), 3)
    _bars_on_sessions(s, iid, sessions, [100.0, 110.0, 99.0])
    out = make_vol_compute(2)(s, "VOLA", iid, [sessions[-1]])
    # returns [+0.10, -0.10] -> pstdev 0.10 -> value = -0.10 * sqrt(252)
    assert out[sessions[-1]] == pytest.approx(-0.10 * math.sqrt(252), abs=1e-12)


def test_vol_split_adjusted_not_fabricated(pg_session):
    s = pg_session
    iid = _instrument(s, "VOLB")
    sessions = _sessions_ending(date(2026, 7, 17), 3)
    # raw closes halve on the split day: adjusted [50, 55, 55] -> returns
    # [+0.10, 0.0] -> pstdev 0.05; unadjusted would fabricate a -50% return
    _bars_on_sessions(s, iid, sessions, [100.0, 110.0, 55.0])
    _split(s, iid, sessions[-1], "2")
    out = make_vol_compute(2)(s, "VOLB", iid, [sessions[-1]])
    assert out[sessions[-1]] == pytest.approx(-0.05 * math.sqrt(252), abs=1e-12)


def test_vol_gap_and_nonpositive_fail_closed(pg_session):
    s = pg_session
    iid = _instrument(s, "VOLC")
    sessions = _sessions_ending(date(2026, 7, 17), 4)
    # gap: only 3 of the 4 sessions have closes -> no value at t
    _bars_on_sessions(s, iid, [sessions[0], sessions[2], sessions[3]],
                      [100.0, 101.0, 102.0])
    assert make_vol_compute(3)(s, "VOLC", iid, [sessions[-1]]) == {}
    # nonpositive close inside the window -> absent, never guessed
    iid2 = _instrument(s, "VOLD")
    _bars_on_sessions(s, iid2, sessions, [100.0, -1.0, 101.0, 102.0])
    assert make_vol_compute(3)(s, "VOLD", iid2, [sessions[-1]]) == {}


# ------------------------------------------- the production anchor ----------

def test_anchor_window20_byte_identical_to_risk_panel(pg_session):
    """make_vol_compute(20) must reproduce research/stock_models.py's
    vol_20d_ann EXACTLY (same Decimal->adjust->float->pstdev chain) on a
    split-bearing, session-exact series — the family's equivalent of the
    momentum byte-identity anchor. The split sits INSIDE the compared 21-close
    window (sessions[50] of 60; the window spans indices 39..59, and
    adjust_for_splits divides bars dated BEFORE the action date, so indices
    39..49 carry factor 2 on BOTH sides of the bare == below) — the identity
    is witnessed THROUGH the adjusted path, not vacuously beside it."""
    s = pg_session
    iid = _instrument(s, "VOLE")
    end = date(2026, 7, 17)
    sessions = _sessions_ending(end, 60)
    # odd closes so factor-2 division produces non-terminating binary floats —
    # a last-ulp divergence between the two adjustment paths cannot hide
    closes = [100.0 + 7.0 * math.sin(i / 3.0) + i * 0.3
              for i in range(len(sessions))]
    _bars_on_sessions(s, iid, sessions, closes)
    _split(s, iid, sessions[50], "2")          # INSIDE the compared window

    models = compute_models(s, iid, "VOLE", end)
    prod = models["risk"]["vol_20d_ann"]
    assert prod is not None
    ours = make_vol_compute(20)(s, "VOLE", iid, [end])[end]
    assert ours == -prod                        # byte-identical, sign-flipped


def test_split_cap_no_look_ahead_per_session(pg_session):
    """A split dated AFTER the target session must not reach that session's
    value (action_date <= t cap, per session): computing the same series at
    t_before and t_after a split must apply it only at t_after."""
    s = pg_session
    iid = _instrument(s, "VOLF")
    sessions = _sessions_ending(date(2026, 7, 17), 8)
    # constant-vol series raw; the split halves the last two raw closes
    raw = [100.0, 102.0, 104.04, 106.12, 108.24, 110.41, 56.31, 57.44]
    _bars_on_sessions(s, iid, sessions, raw)
    _split(s, iid, sessions[6], "2")
    compute = make_vol_compute(3)
    t_before, t_after = sessions[5], sessions[7]
    out = compute(s, "VOLF", iid, [t_before, t_after])
    # at t_before the split (dated later) must be INVISIBLE: raw closes only
    pre = [raw[2], raw[3], raw[4], raw[5]]
    rets_pre = [pre[i] / pre[i - 1] - 1.0 for i in range(1, 4)]
    assert out[t_before] == -(statistics.pstdev(rets_pre) * math.sqrt(252))
    # at t_after the split applies to bars BEFORE its date
    post = [raw[4] / 2.0, raw[5] / 2.0, raw[6], raw[7]]
    rets_post = [post[i] / post[i - 1] - 1.0 for i in range(1, 4)]
    assert out[t_after] == pytest.approx(
        -(statistics.pstdev(rets_post) * math.sqrt(252)), rel=1e-12)


def test_window252_value_golden_with_in_window_split(pg_session):
    """The actually-pinned catalog member's math, tested at its OWN parameters
    (window 252) against an INDEPENDENT in-test recomputation, with a split
    inside the window — a degenerate or non-discriminating low_vol_252 cannot
    hide behind the small-window goldens."""
    s = pg_session
    iid = _instrument(s, "VOLG")
    end = date(2026, 7, 17)
    sessions = _sessions_ending(end, 253)
    closes = [90.0 + 11.0 * math.sin(i / 5.0) + i * 0.07
              for i in range(len(sessions))]
    _bars_on_sessions(s, iid, sessions, closes)
    _split(s, iid, sessions[200], "2")         # inside the 253-close window

    got = LOW_VOL_252.compute(s, "VOLG", iid, [end])[end]
    # independent recomputation of the CHAIN on the STORED inputs: the close
    # column is numeric(_,6), so storage quantizes every price to 6dp — both
    # production paths read those quantized values, and so must the
    # independent math (inputs from storage; adjustment/returns/pstdev
    # recomputed here without touching the module under test)
    stored = [float(r[0]) for r in s.execute(text(
        "SELECT close FROM market.price_bars_daily WHERE instrument_id = :i "
        "ORDER BY bar_date"), {"i": iid})]
    assert len(stored) == 253
    adj = [c / 2.0 if i < 200 else c for i, c in enumerate(stored)]
    rets = [adj[i] / adj[i - 1] - 1.0 for i in range(1, len(adj))]
    expected = -(statistics.pstdev(rets) * math.sqrt(252))
    assert got == expected                     # exact — same inputs, same chain
    assert got < -0.01                          # a real, discriminating number


# ------------------------------------------------- catalog + lineage --------

def test_catalog_carries_low_vol_with_bound_lineage():
    assert "low_vol_252" in RANKABLE_FEATURES
    assert RANKABLE_FEATURES["low_vol_252"] is LOW_VOL_252
    assert FEATURE_LINEAGE["low_vol_252"] == "low-vol"
    assert LOW_VOL_252.spec["window_sessions"] == 252
    assert LOW_VOL_252.spec["estimator"] == "population_stdev"
    # the family module, the math module AND the adjustment are the pin —
    # dropping any of them would let its math drift outside the sha
    paths = [p.name for p in LOW_VOL_252.code_paths]
    assert "low_vol.py" in paths and "volatility.py" in paths
    assert "adjustment.py" in paths


def test_spec_refuses_wrong_lineage_for_low_vol():
    with pytest.raises(Exception, match="lineage"):
        RecipeSpec(name="lv-wrong-line", rank_feature="low_vol_252",
                   direction="desc", top_n=5, rebalance="monthly",
                   universe="pit-sp500", lineage="momentum",
                   rationale="lineage must be the binding, not a choice",
                   kill_start=date(2016, 1, 1))


# -------------------------- end-to-end: the widened catalog is runnable -----

def test_recipe_trial_ranks_low_vol_on_fixture_world(pg_session):
    """Runner evidence for the reviewed widening: a low_vol_252 recipe runs
    the real trial path on the seeded fixture world, registers against the
    'low-vol' lineage at its own count, and lands a verbatim verdict."""
    s = pg_session
    _seed_ziv_world(s)
    audit = PostgresAuditLog(s, _CLOCK)
    spec = RecipeSpec(
        name="lowvol-ziv-check", rank_feature="low_vol_252", direction="desc",
        top_n=5, rebalance="monthly", universe="pit-sp500", lineage="low-vol",
        rationale="Low-volatility anomaly (Ang et al. 2006; Baker-Bradley-"
                  "Wurgler 2011): defensive names earn more than their risk "
                  "due; fixture-world runner evidence for the widening.",
        kill_start=date(2013, 1, 2))
    rec = run_recipe(s, audit, spec, clock=_CLOCK, paths=4, seed=7)

    row = s.execute(text(
        "SELECT lineage, hypothesis, spec_hash FROM quant.trial_registry "
        "WHERE strategy_family = 'recipe-lowvol-ziv-check'")).one()
    assert row.lineage == "low-vol"
    assert "Low-volatility anomaly" in row.hypothesis
    assert row.spec_hash == spec.spec_hash()
    assert rec.n_trials >= 1                    # its own line's count
    assert rec.gate.passed in (True, False)     # a verbatim verdict landed
    # stored values are negative vol (defensive-first under rank-desc)
    vals = [float(r[0]) for r in s.execute(text(
        "SELECT fv.value FROM quant.feature_values fv "
        "JOIN quant.feature_definitions fd ON fd.id = fv.feature_id "
        "WHERE fd.name = 'low_vol_252' LIMIT 20"))]
    assert vals and all(v <= 0 for v in vals)


# ------------------------------------------------------- repin tool ---------

def test_repin_updates_sha_and_audits(clean_audit):
    s = clean_audit
    register_feature(s, LOW_VOL_252, clock=_CLOCK)
    s.execute(text("UPDATE quant.feature_definitions SET code_sha = :x "
                   "WHERE name = 'low_vol_252'"), {"x": "0" * 64})
    line = repin_feature(s, _CLOCK, "low_vol_252", reason="unit repin check")
    assert "repinned" in line
    assert "no stored values existed" in line      # honest about what it checked
    sha = s.execute(text("SELECT code_sha FROM quant.feature_definitions "
                         "WHERE name = 'low_vol_252'")).scalar()
    assert sha == LOW_VOL_252.code_sha()
    ev = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'feature.repinned'")).scalar()
    assert ev["reason"] == "unit repin check"
    assert ev["old_code_sha"] == "0" * 64
    assert ev["stored_values_verified"] == 0
    # per-file digests: the repin is independently verifiable vs any checkout
    assert set(ev["file_digests"]) == {p.name for p in LOW_VOL_252.code_paths}
    # idempotent: a current pin is reported, not re-audited
    assert "already current" in repin_feature(s, _CLOCK, "low_vol_252",
                                              reason="again")


def test_repin_verifies_stored_values_and_refuses_math_drift(pg_session):
    """The backdoor check: stored facts that the CURRENT code cannot reproduce
    byte-for-byte mean the math changed under an unchanged spec — the repin is
    REFUSED. Stored facts that do reproduce are counted in the report."""
    s = pg_session
    iid = _instrument(s, "VOLH")
    sessions = _sessions_ending(date(2026, 7, 17), 3)
    _bars_on_sessions(s, iid, sessions, [100.0, 110.0, 99.0])
    fid = register_feature(s, LOW_VOL_252, clock=_CLOCK)

    def _store(value: float, vintage: str) -> None:
        s.execute(text(
            "INSERT INTO quant.feature_values (feature_id, instrument_id, "
            " session_date, value, dataset_version, computed_at) "
            "VALUES (:f, :i, :d, :v, :dv, :ts)"),
            {"f": fid, "i": iid, "d": sessions[-1], "v": value, "dv": vintage,
             "ts": _CLOCK.now()})

    # a stored fact the current code CANNOT reproduce -> refuse
    _store(-999.0, "vintage-bad")
    s.execute(text("UPDATE quant.feature_definitions SET code_sha = '1' || "
                   "repeat('0', 63) WHERE id = :f"), {"f": fid})
    with pytest.raises(RepinRefused, match="does not reproduce"):
        repin_feature(s, _CLOCK, "low_vol_252", reason="x")

    # replace with the true value (what compute yields at window 2? no — the
    # member's window is 252; on a 3-bar series compute returns NOTHING, which
    # also refuses: an unreproducible stored fact either way)
    s.execute(text("DELETE FROM quant.feature_values WHERE feature_id = :f"),
              {"f": fid})
    # seed a full 253-session series so the member itself can reproduce a fact
    iid2 = _instrument(s, "VOLI")
    long_sessions = _sessions_ending(date(2026, 7, 17), 253)
    _bars_on_sessions(s, iid2, long_sessions,
                      [100.0 + 0.1 * i for i in range(253)])
    true_val = LOW_VOL_252.compute(s, "VOLI", iid2,
                                   [long_sessions[-1]])[long_sessions[-1]]
    s.execute(text(
        "INSERT INTO quant.feature_values (feature_id, instrument_id, "
        " session_date, value, dataset_version, computed_at) "
        "VALUES (:f, :i, :d, :v, 'vintage-good', :ts)"),
        # Decimal(str()) — the store's own bind discipline (store.py): a raw
        # float bind is 15-digit truncated by float8->numeric and would be
        # honestly refused as unreproducible
        {"f": fid, "i": iid2, "d": long_sessions[-1],
         "v": Decimal(str(true_val)), "ts": _CLOCK.now()})
    line = repin_feature(s, _CLOCK, "low_vol_252", reason="verified repin")
    assert "1 stored fact(s) reproduced byte-for-byte" in line


def test_repin_refuses_version_and_spec_drift(pg_session):
    s = pg_session
    register_feature(s, LOW_VOL_252, clock=_CLOCK)
    s.execute(text("UPDATE quant.feature_definitions SET version = '9.9.9' "
                   "WHERE name = 'low_vol_252'"))
    with pytest.raises(RepinRefused, match="version"):
        repin_feature(s, _CLOCK, "low_vol_252", reason="x")
    s.execute(text("UPDATE quant.feature_definitions SET version = :v, "
                   "spec = jsonb_set(spec, '{window_sessions}', '99') "
                   "WHERE name = 'low_vol_252'"), {"v": LOW_VOL_252.version})
    with pytest.raises(RepinRefused, match="MATH"):
        repin_feature(s, _CLOCK, "low_vol_252", reason="x")


def test_repin_refuses_unknown_and_skips_unregistered(pg_session):
    s = pg_session
    with pytest.raises(RepinRefused, match="not in the catalog"):
        repin_feature(s, _CLOCK, "sharpe_maximizer_9000", reason="x")
    assert "not registered" in repin_feature(s, _CLOCK, "momentum_6_1",
                                             reason="x")
