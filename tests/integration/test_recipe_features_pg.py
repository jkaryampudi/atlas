"""Factory momentum family on the point-in-time store (Research Factory
phase 1) — the store-side pins.

Pillars:
1. The generalized family compute at the CANONICAL grid point (252, 21) is
   BYTE-IDENTICAL to phase-1 compute_momentum (dict equality on exact
   floats) — the parameterization cannot drift from the pinned math.
2. Family members materialize through the unchanged store write path and
   read back exact golden literals (Decimal(str(v)) bind discipline).
3. PIT no-look-ahead PROPERTY: perturbing the world strictly AFTER session i
   — an existing later bar REVISED in place (the UPDATE's rowcount is
   asserted, so the perturbation provably lands; a fabricated INSERT would
   PK-conflict into a silent no-op against this densely-seeded fixture) AND
   a 10:1 split — changes neither the value at i nor the dataset_version
   (rows dated after END cannot reach any value knowable by END).
4. dataset_version honesty: identical inputs re-materialize to the SAME
   version as a no-op; a backfilled bar dated <= END produces a NEW version
   (new facts never overwrite old ones); and an IN-PLACE revision of a bar
   <= END — extent unchanged, so the version CANNOT move — is REFUSED loudly
   at re-materialization (the store's stale-fact guard: an append-only store
   never silently re-serves a stale fact).

Every fixture row is written INSIDE the test transaction (rolled back at
teardown), so the store sees exactly this test's world."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.factory.features import RANKABLE_FEATURES, _make_compute
from atlas.dcp.features.definitions import MOMENTUM_12_1
from atlas.dcp.features.momentum import compute_momentum
from atlas.dcp.features.store import dataset_version_for, feature_at, materialize
from atlas.dcp.market_data.calendars import trading_days_between
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2025, 7, 1, 8, 0, tzinfo=UTC))
SEED_START, SEED_END = date(2024, 4, 1), date(2025, 6, 30)
T = date(2025, 5, 30)                     # the probed session (a month end)
SESSIONS = [date(2025, 5, 28), date(2025, 5, 29), T]


def _instrument(s, sym):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency, is_active) "
        "VALUES (:s, 'XTEST', 'US', 'stock', :s, 'USD', true) RETURNING id"),
        {"s": sym}).scalar()


def _seed_bars(s, iid, dates, price):
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, 1000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": price(i)} for i, d in enumerate(dates)])


@pytest.fixture
def family_panel(pg_session):
    """Two deterministic series over the real US calendar: a clean riser and
    a 2:1 split mid-window (the split-cap channel is exercised)."""
    s = pg_session
    cal = trading_days_between("US", SEED_START, SEED_END)
    a = _instrument(s, "FMA")
    _seed_bars(s, a, cal, lambda i: Decimal("100") + Decimal("0.25") * i)
    b = _instrument(s, "FMB")
    _seed_bars(s, b, cal, lambda i: Decimal("50") + Decimal("0.10") * i)
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_date, "
        "action_type, ratio, source) "
        "VALUES (:iid, '2025-02-18', 'split', 2, 'test')"), {"iid": b})
    return s, {"FMA": a, "FMB": b}


# ------------------- 1. the generalized compute pins to the canonical math

def test_family_compute_at_canonical_point_equals_phase1_byte_identical(
        family_panel):
    """_make_compute(252, 21) == compute_momentum, dict-equal on exact
    floats, split channel included — pinned to the literals the fixture math
    produced when the pin was cut (FMA matches the phase-1 equivalence
    fixture's riser exactly)."""
    s, iids = family_panel
    for sym, iid in iids.items():
        gen = _make_compute(252, 21)(s, sym, iid, SESSIONS)
        ph1 = compute_momentum(s, sym, iid, SESSIONS)
        assert gen == ph1, f"{sym}: generalized {gen!r} != phase-1 {ph1!r}"
    assert _make_compute(252, 21)(s, "FMA", iids["FMA"], SESSIONS) == {
        date(2025, 5, 28): 0.5273972602739727,
        date(2025, 5, 29): 0.5261958997722096,
        T: 0.5249999999999999}
    assert _make_compute(252, 21)(s, "FMB", iids["FMB"], SESSIONS) == {
        date(2025, 5, 28): 1.8587360594795541,
        date(2025, 5, 29): 1.8571428571428572,
        T: 1.8555555555555552}


# --------------------------- 2. members materialize to golden literals

def test_family_members_materialize_golden_pins(family_panel):
    s, _ = family_panel
    m61 = RANKABLE_FEATURES["momentum_6_1"]
    rep = materialize(s, m61, clock=CLOCK, symbols=["FMA", "FMB"],
                      sessions=SESSIONS)
    assert rep.failed == ()
    assert rep.computed == {"FMA": 3, "FMB": 3}
    assert feature_at(s, m61, "FMA", on=T,
                      dataset_version=rep.dataset_version) == \
        0.18551236749116606
    assert feature_at(s, m61, "FMB", on=T,
                      dataset_version=rep.dataset_version) == \
        1.3153153153153152

    m120 = RANKABLE_FEATURES["momentum_12_0"]
    rep0 = materialize(s, m120, clock=CLOCK, symbols=["FMA"], sessions=[T])
    assert feature_at(s, m120, "FMA", on=T,
                      dataset_version=rep0.dataset_version) == \
        0.5727272727272728

    rep1 = materialize(s, MOMENTUM_12_1, clock=CLOCK, symbols=["FMA"],
                       sessions=[T])
    assert feature_at(s, MOMENTUM_12_1, "FMA", on=T,
                      dataset_version=rep1.dataset_version) == \
        0.5249999999999999   # the phase-1 equivalence fixture's exact pin


# ------------------------------- 3. PIT no-look-ahead property (perturb > i)

def test_perturbing_bars_after_i_changes_nothing_at_i(family_panel):
    """Materialize at i, then perturb the world strictly AFTER i — REVISE an
    existing later bar to a crazy value AND fabricate a 10:1 split: the
    value at i and the dataset_version must both be unchanged — rows dated
    after END are structurally invisible to values knowable by END.
    Exercised for a canonical member and a family member alike.

    The bar channel is an UPDATE with its rowcount asserted (the fixture
    seeds a bar on every US session, so the old fabricated INSERT ... ON
    CONFLICT DO NOTHING was a silent no-op: the channel was never actually
    perturbed); the cleanup is scoped to exactly the touched rows."""
    s, iids = family_panel
    later = date(2025, 6, 16)                        # a US session after T
    assert later > T
    original = s.execute(text(
        "SELECT close FROM market.price_bars_daily "
        "WHERE instrument_id = :iid AND bar_date = :d"),
        {"iid": iids["FMA"], "d": later}).scalar_one()   # dense fixture: exists
    for feature in (MOMENTUM_12_1, RANKABLE_FEATURES["momentum_6_1"]):
        before = materialize(s, feature, clock=CLOCK,
                             symbols=["FMA", "FMB"], sessions=SESSIONS)
        vals_before = {
            sym: feature_at(s, feature, sym, on=T,
                            dataset_version=before.dataset_version)
            for sym in ("FMA", "FMB")}
        assert all(v is not None for v in vals_before.values())

        landed = s.execute(text(
            "UPDATE market.price_bars_daily SET open = 1, high = 1, "
            "low = 1, close = 1, volume = 9 "
            "WHERE instrument_id = :iid AND bar_date = :d"),
            {"iid": iids["FMA"], "d": later}).rowcount
        assert landed == 1              # the bar perturbation genuinely landed
        landed = s.execute(text(
            "INSERT INTO market.corporate_actions (instrument_id, "
            "action_date, action_type, ratio, source) "
            "VALUES (:iid, :d, 'split', 10, 'test')"),
            {"iid": iids["FMB"], "d": later}).rowcount
        assert landed == 1              # the split perturbation too

        after = materialize(s, feature, clock=CLOCK,
                            symbols=["FMA", "FMB"], sessions=SESSIONS)
        assert after.dataset_version == before.dataset_version
        assert after.inserted == 0                   # pure no-op re-run
        for sym, expected in vals_before.items():
            assert feature_at(s, feature, sym, on=T,
                              dataset_version=after.dataset_version) == expected
        # restore EXACTLY the touched rows for the next feature's round
        s.execute(text(
            "UPDATE market.price_bars_daily SET open = :c, high = :c, "
            "low = :c, close = :c, volume = 1000 "
            "WHERE instrument_id = :iid AND bar_date = :d"),
            {"iid": iids["FMA"], "d": later, "c": original})
        s.execute(text(
            "DELETE FROM market.corporate_actions "
            "WHERE instrument_id = :iid AND action_date = :d "
            "  AND action_type = 'split'"),
            {"iid": iids["FMB"], "d": later})


# --------------------------------------- 4. dataset_version honesty

def test_dataset_version_deterministic_and_reversioned_on_backfill(
        family_panel):
    s, iids = family_panel
    m61 = RANKABLE_FEATURES["momentum_6_1"]
    first = materialize(s, m61, clock=CLOCK, symbols=["FMA", "FMB"],
                        sessions=SESSIONS)
    again = materialize(s, m61, clock=CLOCK, symbols=["FMA", "FMB"],
                        sessions=SESSIONS)
    assert again.dataset_version == first.dataset_version
    assert again.inserted == 0 and again.existing == first.inserted

    # a BACKFILLED row dated <= END re-versions the dataset: new facts land
    # beside the old ones, never over them
    earlier = date(2024, 3, 28)                      # US session before start
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, 99, 99, 99, 99, 1000, 'EodhdAdapter')"),
        {"iid": iids["FMA"], "d": earlier})
    rev = materialize(s, m61, clock=CLOCK, symbols=["FMA", "FMB"],
                      sessions=SESSIONS)
    assert rev.dataset_version != first.dataset_version
    old_rows = s.execute(text(
        "SELECT count(*) FROM quant.feature_values fv "
        "JOIN quant.feature_definitions fd ON fd.id = fv.feature_id "
        "WHERE fd.name = 'momentum_6_1' AND fv.dataset_version = :dv"),
        {"dv": first.dataset_version}).scalar()
    assert old_rows == first.inserted                # history never rewritten


def test_in_place_bar_revision_same_extent_refused_at_materialize(
        family_panel):
    """The stale-fact guard: revise a formation-window PROBE bar IN PLACE —
    the production ingest upsert changes no per-symbol min/max/count, so the
    dataset_version CANNOT move — and re-materialize. The recomputed value
    now differs from the stored fact under the SAME version, and materialize
    must fail LOUD, naming the row: an append-only store never silently
    re-serves a stale fact (before this guard, ON CONFLICT DO NOTHING
    discarded the fresh value and the runner's fail-loud check saw
    failed=() while ranking on the pre-revision world)."""
    s, iids = family_panel
    m61 = RANKABLE_FEATURES["momentum_6_1"]
    first = materialize(s, m61, clock=CLOCK, symbols=["FMA", "FMB"],
                        sessions=SESSIONS)
    assert first.failed == ()

    cal = trading_days_between("US", SEED_START, SEED_END)
    skip_date = cal[cal.index(T) - 21]     # T's c_skip probe (skip=21)
    landed = s.execute(text(
        "UPDATE market.price_bars_daily SET close = close * 2 "
        "WHERE instrument_id = :iid AND bar_date = :d"),
        {"iid": iids["FMA"], "d": skip_date}).rowcount
    assert landed == 1                     # the in-place revision landed

    same_extent = m61.input_extent(s, ["FMA", "FMB"], max(SESSIONS))
    assert dataset_version_for(m61, same_extent) == first.dataset_version

    with pytest.raises(RuntimeError, match="stale stored fact") as exc:
        materialize(s, m61, clock=CLOCK, symbols=["FMA", "FMB"],
                    sessions=SESSIONS)
    # the error names the row: feature, symbol, session, dataset_version
    msg = str(exc.value)
    assert "momentum_6_1" in msg and "FMA" in msg
    assert str(T) in msg and first.dataset_version in msg


def test_uncomputable_revision_orphans_stored_fact_and_is_refused(
        family_panel):
    """The stale-fact guard's ORPHAN direction (adversarial re-attack
    2026-07-18): revise T's FORM probe close to 0 IN PLACE. The extent hash
    provably cannot move (the extent counts close IS NOT NULL rows, and 0 is
    not NULL), but the fresh compute now fail-closes at T (c_form <= 0) and
    produces NO value there — so the value-comparison loop never visits T,
    and before this guard the stored pre-revision fact under the SAME
    dataset_version would have been silently re-served by feature_panel to
    the ranking. materialize must fail LOUD, naming the orphaned row."""
    s, iids = family_panel
    m61 = RANKABLE_FEATURES["momentum_6_1"]
    first = materialize(s, m61, clock=CLOCK, symbols=["FMA", "FMB"],
                        sessions=SESSIONS)
    assert first.failed == ()

    cal = trading_days_between("US", SEED_START, SEED_END)
    form_date = cal[cal.index(T) - 126]    # T's c_form probe (lookback=126)
    landed = s.execute(text(
        "UPDATE market.price_bars_daily "
        "SET open = 0, high = 0, low = 0, close = 0 "
        "WHERE instrument_id = :iid AND bar_date = :d"),
        {"iid": iids["FMA"], "d": form_date}).rowcount
    assert landed == 1                     # the in-place revision landed

    same_extent = m61.input_extent(s, ["FMA", "FMB"], max(SESSIONS))
    assert dataset_version_for(m61, same_extent) == first.dataset_version

    with pytest.raises(RuntimeError,
                       match="stale stored fact .orphaned.") as exc:
        materialize(s, m61, clock=CLOCK, symbols=["FMA", "FMB"],
                    sessions=SESSIONS)
    msg = str(exc.value)
    assert "momentum_6_1" in msg and "FMA" in msg
    assert str(T) in msg and first.dataset_version in msg
    assert "uncomputable" in msg
