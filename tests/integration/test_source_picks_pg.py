"""research.source_picks — the external-pick measurement layer.

Pins: (1) the feature snapshot is POINT-IN-TIME (future bars can never change a
snapshot taken at an earlier as_of); (2) recording is idempotent and NEVER
writes a committee memo (invariant 2 — an external pick is not a BUY memo);
(3) grading computes excess vs SPY the scorecard's way and is WRITE-ONCE;
(4) the per-source edge report scores outperform-rate against the dartboard.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.research.source_picks import (
    PICK_FEATURE_VERSION,
    grade_picks,
    record_pick,
    snapshot_features,
    source_edge_report,
)
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 18, 22, tzinfo=UTC))


def _clean(s):
    s.execute(text("TRUNCATE research.source_picks"))
    s.execute(text("DELETE FROM market.price_bars_daily WHERE instrument_id IN "
                   "(SELECT id FROM market.instruments WHERE symbol IN ('SPY','PICKCO'))"))
    s.execute(text("DELETE FROM market.instruments WHERE symbol IN ('SPY','PICKCO')"))


def _instrument(s, sym, sector="Information Technology"):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, instrument_type, "
        " name, currency, is_active, sector_gics) "
        "VALUES (:s,'US','US','stock',:s,'USD',true,:sec) RETURNING id"),
        {"s": sym, "sec": sector}).scalar()


def _seed_bars(s, iid, start: date, closes: list[float]):
    """One bar per business day from `start` (source EodhdAdapter, the loader's
    vendor). Returns the ascending list of bar dates actually written."""
    d, dates = start, []
    for c in closes:
        while d.weekday() >= 5:            # skip weekends -> business-day grid
            d += timedelta(days=1)
        s.execute(text(
            "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
            " high, low, close, volume, source) "
            "VALUES (:i,:d,:c,:c,:c,:c,1000,'EodhdAdapter')"),
            {"i": iid, "d": d, "c": c})
        dates.append(d)
        d += timedelta(days=1)
    return dates


def test_snapshot_is_point_in_time(pg_session):
    s = pg_session
    _clean(s)
    spy = _instrument(s, "SPY")
    pick = _instrument(s, "PICKCO")
    start = date(2025, 1, 1)
    # 300 rising closes for both; as_of at index 260.
    pick_closes = [100.0 + i for i in range(300)]
    spy_closes = [400.0 + i * 0.5 for i in range(300)]
    pdates = _seed_bars(s, pick, start, pick_closes)
    _seed_bars(s, spy, start, spy_closes)
    as_of = pdates[260]

    snap1 = snapshot_features(s, pick, "PICKCO", as_of)
    assert snap1["feature_version"] == PICK_FEATURE_VERSION
    assert snap1["mom_12_1"] is not None and snap1["ret_20d"] is not None
    assert snap1["sector_gics"] == "Information Technology"

    # perturb the FUTURE (bars strictly after as_of) with wild values, re-snap
    # at the SAME as_of -> byte-identical: no look-ahead is structural.
    s.execute(text("UPDATE market.price_bars_daily SET close = close * 5 "
                   "WHERE instrument_id = :i AND bar_date > :a"),
              {"i": pick, "a": as_of})
    snap2 = snapshot_features(s, pick, "PICKCO", as_of)
    assert snap2 == snap1


def test_record_is_idempotent_and_writes_no_memo(pg_session):
    s = pg_session
    _clean(s)
    _instrument(s, "SPY")
    pick = _instrument(s, "PICKCO")
    start = date(2025, 1, 1)
    dates = _seed_bars(s, pick, start, [100.0 + i for i in range(300)])
    _seed_bars(s, _sym_id(s, "SPY"), start, [400.0 + i for i in range(300)])
    rd = dates[270]
    memos_before = s.execute(text("SELECT count(*) FROM research.memos")).scalar()

    first = record_pick(s, source="investing.com", ticker="PICKCO",
                        instrument_id=pick, recommendation_date=rd, as_of_session=dates[270])
    assert first is not None
    dup = record_pick(s, source="investing.com", ticker="PICKCO",
                      instrument_id=pick, recommendation_date=rd, as_of_session=dates[270])
    assert dup is None                                       # idempotent
    assert s.execute(text("SELECT count(*) FROM research.source_picks")).scalar() == 1
    # invariant 2: an external pick is NEVER a committee memo.
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() == memos_before


def test_grade_is_scorecard_excess_and_write_once(pg_session):
    s = pg_session
    _clean(s)
    spy = _instrument(s, "SPY")
    pick = _instrument(s, "PICKCO")
    start = date(2024, 1, 1)
    # pick clearly OUTperforms SPY over the forward window.
    pdates = _seed_bars(s, pick, start, [100.0 * (1.004 ** i) for i in range(340)])
    _seed_bars(s, spy, start, [400.0 * (1.001 ** i) for i in range(340)])
    anchor_idx = 270
    rd = pdates[anchor_idx]
    record_pick(s, source="investing.com", ticker="PICKCO", instrument_id=pick,
                recommendation_date=rd, as_of_session=rd)

    g = grade_picks(s, CLOCK)
    assert g.graded == 2                                    # both horizons matured
    row = s.execute(text("SELECT excess_20, excess_60, graded_at FROM research.source_picks")
                    ).mappings().one()
    # pick grew 0.4%/session vs SPY 0.1% -> excess strongly positive (outperformed)
    assert row["excess_20"] > 0 and row["excess_60"] > row["excess_20"]
    assert row["graded_at"] is not None

    # WRITE-ONCE: re-grading changes nothing (a matured outcome is a fact).
    before = (row["excess_20"], row["excess_60"])
    g2 = grade_picks(s, CLOCK)
    assert g2.graded == 0
    after = s.execute(text("SELECT excess_20, excess_60 FROM research.source_picks")
                      ).mappings().one()
    assert (after["excess_20"], after["excess_60"]) == before


def test_source_edge_report_scores_against_dartboard(pg_session):
    s = pg_session
    _clean(s)
    spy = _instrument(s, "SPY")
    _seed_bars(s, spy, date(2024, 1, 1), [400.0 * (1.001 ** i) for i in range(340)])
    # three picks: two outperform, one underperforms -> outperform rate 2/3.
    specs = [("AAA", 1.004, True), ("BBB", 1.003, True), ("CCC", 0.999, False)]
    for sym, g, _ in specs:
        iid = _instrument(s, sym)
        dates = _seed_bars(s, iid, date(2024, 1, 1), [100.0 * (g ** i) for i in range(340)])
        record_pick(s, source="investing.com", ticker=sym, instrument_id=iid,
                    recommendation_date=dates[270], as_of_session=dates[270])
    grade_picks(s, CLOCK)

    rep = {(e.source, e.horizon): e for e in source_edge_report(s)}
    e20 = rep[("investing.com", 20)]
    assert e20.n_matured == 3
    assert abs(e20.outperform_rate - 2 / 3) < 1e-9
    assert e20.dartboard is not None and e20.edge is not None


def _sym_id(s, sym):
    return s.execute(text("SELECT id FROM market.instruments WHERE symbol=:s"),
                     {"s": sym}).scalar()
