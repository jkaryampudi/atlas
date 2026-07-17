"""Feature-store contract (ADR-0011 step 1): registration pins, structural
no-look-ahead reads, dataset_version determinism and the append-only
convention. SUE is the workhorse fixture (earnings rows are light to seed);
byte-identity against the production signal code lives in
test_feature_equivalence_pg.py."""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.features.definitions import SUE_FOS
from atlas.dcp.features.store import (
    FeatureDefinition,
    FeaturePinError,
    dataset_version_for,
    feature_at,
    feature_panel,
    latest_dataset_version,
    materialize,
    register_feature,
)
from atlas.dcp.market_data.calendars import trading_days_between
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2025, 7, 1, 8, 0, tzinfo=UTC))
LATER = FrozenClock(datetime(2025, 7, 2, 8, 0, tzinfo=UTC))

# SUEA fixture from the equivalence suite: 10 quarterly reports, estimate
# 1.00, surprises 0.10 0.20 -0.10 0.05 0.15 0.30 -0.05 0.10 0.20 0.40 —
# reports 0-3 have <4 priors (SUE undefined), report 4 onward defined.
SURPRISES = ("0.10", "0.20", "-0.10", "0.05", "0.15",
             "0.30", "-0.05", "0.10", "0.20", "0.40")


def _instrument(s, sym):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency, is_active) "
        "VALUES (:s, 'XTEST', 'US', 'stock', :s, 'USD', true) RETURNING id"),
        {"s": sym}).scalar()


def _quarters(first: date, n: int) -> list[date]:
    out, y, m = [], first.year, first.month
    days = {3: 31, 6: 30, 9: 30, 12: 31}
    for _ in range(n):
        out.append(date(y, m, days[m]))
        m += 3
        if m > 12:
            m, y = m - 12, y + 1
    return out


def _seed_reports(s, iid, fpes, surprises, *, whens=None):
    for i, (fpe, surp) in enumerate(zip(fpes, surprises)):
        s.execute(text(
            "INSERT INTO market.earnings_surprises (instrument_id, "
            "fiscal_period_end, report_date, eps_actual, eps_estimate, "
            "surprise_pct, currency, before_after_market, source, fetched_at) "
            "VALUES (:iid, :fpe, :rd, :a, '1.00', NULL, 'USD', :w, 'test', :fa)"),
            {"iid": iid, "fpe": fpe, "rd": fpe + timedelta(days=45),
             "a": str(Decimal("1.00") + Decimal(surp)),
             "w": whens[i] if whens else "BeforeMarket", "fa": CLOCK.now()})


@pytest.fixture
def seeded(pg_session):
    iid = _instrument(pg_session, "FSA")
    _seed_reports(pg_session, iid, _quarters(date(2022, 12, 31), 10), SURPRISES)
    return pg_session


# ------------------------------------------------------------- registration

def _dummy_feature(tmp_path, **overrides) -> FeatureDefinition:
    src = tmp_path / "dummy_feature.py"
    if not src.exists():
        src.write_text("VALUE = 1\n")
    base = dict(
        name="dummy_feature", version="1.0.0", market="US",
        spec={"param": 1}, code_paths=(src,),
        compute=lambda db, sym, iid, sessions: {},
        input_extent=lambda db, syms, end: {"symbols": {}})
    base.update(overrides)
    return FeatureDefinition(**base)


def test_register_feature_is_idempotent(pg_session, tmp_path):
    feat = _dummy_feature(tmp_path)
    fid = register_feature(pg_session, feat, clock=CLOCK)
    assert register_feature(pg_session, feat, clock=LATER) == fid
    n = pg_session.execute(text(
        "SELECT count(*) FROM quant.feature_definitions "
        "WHERE name = 'dummy_feature'")).scalar()
    assert n == 1


def test_register_feature_refuses_changed_code_sha(pg_session, tmp_path):
    feat = _dummy_feature(tmp_path)
    register_feature(pg_session, feat, clock=CLOCK)
    (tmp_path / "dummy_feature.py").write_text("VALUE = 2  # drifted\n")
    with pytest.raises(FeaturePinError):
        register_feature(pg_session, feat, clock=LATER)


def test_register_feature_refuses_changed_version_or_spec(pg_session, tmp_path):
    feat = _dummy_feature(tmp_path)
    register_feature(pg_session, feat, clock=CLOCK)
    with pytest.raises(FeaturePinError):
        register_feature(pg_session,
                         dataclasses.replace(feat, version="1.1.0"),
                         clock=LATER)
    with pytest.raises(FeaturePinError):
        register_feature(pg_session,
                         dataclasses.replace(feat, spec={"param": 2}),
                         clock=LATER)


def test_registered_code_sha_pins_the_signal_sources(pg_session):
    """The real definitions hash the signal modules they import math from."""
    fid = register_feature(pg_session, SUE_FOS, clock=CLOCK)
    stored = pg_session.execute(text(
        "SELECT code_sha, spec FROM quant.feature_definitions WHERE id = :f"),
        {"f": fid}).one()
    assert stored.code_sha == SUE_FOS.code_sha()
    assert stored.spec["staleness_sessions"] == 63


# ------------------------------------------------- no-look-ahead + carry-0

def test_value_for_session_s_is_invisible_before_s(seeded):
    """Structural no-look-ahead: a value computed for session S must not be
    returned for any on < S, and each session serves ITS OWN value. The two
    sessions straddle a report boundary (report 8 lands BeforeMarket on
    2025-02-14), so their values DIFFER — a read that leaked the later row
    would be caught by value, not just by presence."""
    s = seeded
    s1, s2 = date(2025, 2, 13), date(2025, 2, 14)
    rep = materialize(s, SUE_FOS, clock=CLOCK, symbols=["FSA"],
                      sessions=[s1, s2])
    assert rep.inserted == 2
    v1 = feature_at(s, SUE_FOS, "FSA", on=s1, dataset_version=rep.dataset_version)
    v2 = feature_at(s, SUE_FOS, "FSA", on=s2, dataset_version=rep.dataset_version)
    assert v1 is not None and v2 is not None
    assert v1 != v2                      # different reports, different values
    # 2025-02-12 is a session BEFORE anything materialized: nothing knowable —
    # the s1/s2 values exist in the table but are structurally out of reach
    assert feature_at(s, SUE_FOS, "FSA", on=date(2025, 2, 12),
                      dataset_version=rep.dataset_version) is None


def test_carry_zero_weekend_reads_ok_next_session_stale(seeded):
    s = seeded
    s3 = date(2025, 5, 30)                            # Friday
    rep = materialize(s, SUE_FOS, clock=CLOCK, symbols=["FSA"], sessions=[s3])
    dv = rep.dataset_version
    val = feature_at(s, SUE_FOS, "FSA", on=s3, dataset_version=dv)
    assert val is not None
    # Saturday: zero trading sessions elapsed — Friday's value is still live
    assert feature_at(s, SUE_FOS, "FSA", on=date(2025, 5, 31),
                      dataset_version=dv) == val
    # Monday: one session elapsed — the store refuses to serve stale as fresh
    assert feature_at(s, SUE_FOS, "FSA", on=date(2025, 6, 2),
                      dataset_version=dv) is None


def test_feature_panel_is_bounded_and_pinned(seeded):
    s = seeded
    sessions = trading_days_between("US", date(2025, 5, 27), date(2025, 5, 30))
    rep = materialize(s, SUE_FOS, clock=CLOCK, symbols=["FSA"],
                      sessions=sessions)
    panel = feature_panel(s, SUE_FOS, ["FSA"], start=date(2025, 5, 27),
                          end=date(2025, 5, 29), dataset_version=rep.dataset_version)
    assert set(panel) == {"FSA"}
    assert max(panel["FSA"]) == date(2025, 5, 29)     # end bound respected
    assert date(2025, 5, 30) not in panel["FSA"]
    other = feature_panel(s, SUE_FOS, ["FSA"], start=date(2025, 5, 27),
                          end=date(2025, 5, 30), dataset_version="no-such-vintage")
    assert other == {"FSA": {}}                       # pinned vintage or nothing


# ------------------------------------- dataset_version + append-only facts

def test_dataset_version_deterministic_and_wall_clock_free(seeded):
    s = seeded
    sessions = [date(2025, 5, 29), date(2025, 5, 30)]
    rep1 = materialize(s, SUE_FOS, clock=CLOCK, symbols=["FSA"],
                       sessions=sessions)
    rep2 = materialize(s, SUE_FOS, clock=LATER, symbols=["FSA"],
                       sessions=sessions)
    assert rep1.dataset_version == rep2.dataset_version
    assert rep1.inserted == 2 and rep2.inserted == 0
    assert rep2.existing == 2                         # no-op, never an UPDATE
    stamps = s.execute(text(
        "SELECT DISTINCT computed_at FROM quant.feature_values "
        "WHERE dataset_version = :dv"),
        {"dv": rep1.dataset_version}).scalars().all()
    assert stamps == [CLOCK.now()]                    # originals untouched


def test_new_input_data_creates_new_version_beside_old_facts(seeded):
    s = seeded
    sessions = [date(2025, 5, 30)]
    rep1 = materialize(s, SUE_FOS, clock=CLOCK, symbols=["FSA"],
                       sessions=sessions)
    # a backfilled MID-HISTORY quarter (report_date <= end) changes the input
    # extent — and shifts the SUE standardization window, so the value itself
    # changes — it must re-version, never rewrite
    iid = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'FSA'")).scalar()
    _seed_reports(s, iid, [date(2024, 1, 15)], ["0.07"])
    rep2 = materialize(s, SUE_FOS, clock=LATER, symbols=["FSA"],
                       sessions=sessions)
    assert rep2.dataset_version != rep1.dataset_version
    kept = s.execute(text(
        "SELECT count(*) FROM quant.feature_values "
        "WHERE dataset_version = :dv"), {"dv": rep1.dataset_version}).scalar()
    assert kept == rep1.inserted                      # old facts intact
    v_old = feature_at(s, SUE_FOS, "FSA", on=sessions[0],
                       dataset_version=rep1.dataset_version)
    v_new = feature_at(s, SUE_FOS, "FSA", on=sessions[0],
                       dataset_version=rep2.dataset_version)
    assert v_old is not None and v_new is not None and v_old != v_new
    # the unpinned read resolves to the newest vintage
    assert latest_dataset_version(s, SUE_FOS) == rep2.dataset_version
    assert feature_at(s, SUE_FOS, "FSA", on=sessions[0]) == v_new


def test_data_after_the_extent_end_does_not_reversion(seeded):
    s = seeded
    sessions = [date(2025, 5, 30)]
    rep1 = materialize(s, SUE_FOS, clock=CLOCK, symbols=["FSA"],
                       sessions=sessions)
    iid = s.execute(text(
        "SELECT id FROM market.instruments WHERE symbol = 'FSA'")).scalar()
    _seed_reports(s, iid, [date(2025, 6, 30)], ["0.99"])   # rd 2025-08-14 > end
    rep2 = materialize(s, SUE_FOS, clock=LATER, symbols=["FSA"],
                       sessions=sessions)
    assert rep2.dataset_version == rep1.dataset_version
    assert rep2.inserted == 0


def test_symbol_set_is_part_of_the_dataset_identity(seeded):
    ext_a = SUE_FOS.input_extent(seeded, ["FSA"], date(2025, 5, 30))
    ext_ab = SUE_FOS.input_extent(seeded, ["FSA", "FSB"], date(2025, 5, 30))
    assert (dataset_version_for(SUE_FOS, ext_a)
            != dataset_version_for(SUE_FOS, ext_ab))
