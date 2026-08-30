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
    # F-006: grade_picks now scores on the AUD total-return basis, so both legs
    # pass through fx_to_aud. A FLAT USD->AUD rate seeded before all bars
    # (fx_to_aud carries forward) cancels in every return ratio, leaving the
    # hand-pinned excess numbers unchanged while exercising the reporting path.
    s.execute(text(
        "INSERT INTO market.fx_rates_daily (base, quote, rate_date, rate, source) "
        "VALUES ('USD','AUD', DATE '2020-01-01', '1.0', 'test') "
        "ON CONFLICT (base, quote, rate_date) DO UPDATE SET rate = '1.0'"))
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
    assert g.graded == 4                        # all four horizons matured (5/10/20/60)
    row = s.execute(text(
        "SELECT excess_5, excess_10, excess_20, excess_60, graded_at "
        "FROM research.source_picks")).mappings().one()
    # pick grows 0.4%/session vs SPY 0.1% -> excess is positive and RISES with
    # the horizon (the outperformance compounds over more sessions).
    assert 0 < row["excess_5"] < row["excess_10"] < row["excess_20"] < row["excess_60"]
    assert row["graded_at"] is not None

    # WRITE-ONCE: re-grading changes nothing (a matured outcome is a fact).
    before = tuple(row[c] for c in ("excess_5", "excess_10", "excess_20", "excess_60"))
    g2 = grade_picks(s, CLOCK)
    assert g2.graded == 0
    after = s.execute(text(
        "SELECT excess_5, excess_10, excess_20, excess_60 "
        "FROM research.source_picks")).mappings().one()
    assert tuple(after[c] for c in ("excess_5", "excess_10", "excess_20", "excess_60")) == before


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

    # ONE tracked source: the dart has nothing else to throw at, so the
    # dartboard necessarily equals the source's own rate. The report must
    # say "no comparator" (edge None) — never a fake 0.0 edge.
    rep = {(e.source, e.horizon): e for e in source_edge_report(s)}
    e20 = rep[("investing.com", 20)]
    assert e20.n_matured == 3
    assert abs(e20.outperform_rate - 2 / 3) < 1e-9
    assert e20.dartboard is not None and abs(e20.dartboard - 2 / 3) < 1e-9
    assert e20.edge is None

    # A SECOND source with three underperformers: the dart is now the base
    # rate over all six graded picks (2/6), so investing.com's edge is
    # 2/3 - 1/3 = +1/3 and the other source's is 0 - 1/3. Before the
    # 2026-08-30 fix both would have read 0.0 (each source was scored
    # against its own picks — a tautology).
    for sym in ("DDD", "EEE", "FFF"):
        iid = _instrument(s, sym)
        dates = _seed_bars(s, iid, date(2024, 1, 1), [100.0 * (0.998 ** i) for i in range(340)])
        record_pick(s, source="other-src", ticker=sym, instrument_id=iid,
                    recommendation_date=dates[270], as_of_session=dates[270])
    grade_picks(s, CLOCK)
    rep = {(e.source, e.horizon): e for e in source_edge_report(s)}
    inv, oth = rep[("investing.com", 20)], rep[("other-src", 20)]
    assert inv.dartboard is not None and abs(inv.dartboard - 2 / 6) < 1e-9
    assert oth.dartboard == inv.dartboard            # one dart, thrown at everything
    assert inv.edge is not None and abs(inv.edge - (2 / 3 - 2 / 6)) < 1e-9
    assert oth.edge is not None and abs(oth.edge - (0.0 - 2 / 6)) < 1e-9


def _sym_id(s, sym):
    return s.execute(text("SELECT id FROM market.instruments WHERE symbol=:s"),
                     {"s": sym}).scalar()


class EodhdAdapter:
    """Stub named EodhdAdapter ON PURPOSE (test_scorecard_pg precedent): the
    top-up stores bars under source=type(adapter).__name__, and the grading
    loaders enforce the vendor-bar discipline (source = 'EodhdAdapter') — a
    differently-named stub would store bars grading cannot see. Serves a
    pre-built forward window; records every fetch so the test can assert
    exactly which symbols were topped up."""
    def __init__(self, bars):
        self.bars = bars
        self.calls: list[tuple[str, date, date]] = []

    def fetch_bars(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return [b for b in self.bars if start <= b.bar_date <= end]

    def fetch_splits(self, symbol, start, end):
        return []


def test_grade_tops_up_frozen_inactive_pick_bars(pg_session):
    """The investing.com ARM/HIMX/... defect: a pick on an ANALYSIS-ONLY
    (is_active=false) instrument freezes at its last analyzed bar — nightly
    ingest skips inactive rows and grade_picks had no top-up — so the pick
    never matures and is silently excluded from the source's edge verdict.
    With `adapter_for`, grading first tops up exactly the missing window
    (scorecard rules: inactive only, no backfill) and the pick then grades."""
    from decimal import Decimal as D

    from atlas.dcp.market_data.models import Bar

    s = pg_session
    _clean(s)
    spy = _instrument(s, "SPY")
    pick = _instrument(s, "PICKCO")
    s.execute(text("UPDATE market.instruments SET is_active=false WHERE id=:i"),
              {"i": pick})                       # analysis-only, like ARM/HIMX

    start = date(2026, 4, 1)
    closes = [100.0 * (1.004 ** i) for i in range(80)]
    _seed_bars(s, spy, start, [400.0 * (1.001 ** i) for i in range(80)])
    # PICKCO's stored series FREEZES after 50 bars (the frozen-at-07-17 shape)
    pdates = _seed_bars(s, pick, start, closes[:50])
    frozen_at = pdates[-1]
    rd = pdates[47]                               # only 2 stored forward bars
    record_pick(s, source="investing.com", ticker="PICKCO", instrument_id=pick,
                recommendation_date=rd, as_of_session=rd)

    # WITHOUT the adapter: cannot mature, and nothing is fetched (pure read)
    g0 = grade_picks(s, CLOCK)
    assert g0.topups == ()
    assert s.execute(text("SELECT excess_5 FROM research.source_picks")).scalar() is None

    # the un-stored tail of the same walk, on the same business-day grid
    tail = []
    d, i = start, 0
    while i < 80:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if d > frozen_at:
            tail.append(Bar(symbol="PICKCO", bar_date=d,
                            open=D(str(round(closes[i], 6))),
                            high=D(str(round(closes[i], 6))),
                            low=D(str(round(closes[i], 6))),
                            close=D(str(round(closes[i], 6))), volume=1000))
        d += timedelta(days=1)
        i += 1
    stub = EodhdAdapter(tail)

    g1 = grade_picks(s, CLOCK, adapter_for=lambda sym, exch: stub)
    # exactly the inactive pick symbol was topped up, only its missing window
    assert [c[0] for c in stub.calls] == ["PICKCO"]
    assert stub.calls[0][1] > frozen_at
    assert len(g1.topups) == 1 and "PICKCO" in g1.topups[0]
    new_last = s.execute(text(
        "SELECT max(bar_date) FROM market.price_bars_daily WHERE instrument_id=:i"),
        {"i": pick}).scalar()
    assert new_last > frozen_at                   # the freeze is over
    # and the pick now actually matures (the whole point)
    assert g1.graded >= 1
    assert s.execute(text("SELECT excess_5 FROM research.source_picks")).scalar() is not None
