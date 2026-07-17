"""EQUIVALENCE golden pins (ADR-0011 step 1 — the load-bearing tests).

Before anything may depend on the feature store, the store must be proven a
FAITHFUL substrate: for a fixture panel, feature_at must return values
BYTE-IDENTICAL (== on floats, no tolerance) to what the existing signal code
computes —

  momentum_12_1            vs signals/xsmom/generate._formation_returns
                              (the production ranker's formation return:
                              Decimal closes -> split adjust -> float divide),
  sue_foster_olsen_shevlin vs signals/pead/v1 EarningsView.live() dense at
                              EVERY session (built here over a DIFFERENT,
                              longer panel calendar — the index-invariance the
                              store relies on is itself under test), and
                              vs signals/pead/generate._live_sues at a signal
                              session (the full production read path).

The pinned literals were computed once from the deterministic fixtures and
hardcoded (golden pins): any drift in the imported math, the store's write
path (e.g. a float bind truncating through numeric) or the read path breaks
an exact equality here.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from atlas.core.clock import FrozenClock
from atlas.dcp.features.definitions import MOMENTUM_12_1, SUE_FOS
from atlas.dcp.features.store import feature_at, materialize
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.earnings_history import EarningsSurprise
from atlas.dcp.signals.pead.generate import _live_sues
from atlas.dcp.signals.pead.v1 import build_earnings_view
from atlas.dcp.signals.xsmom.generate import _formation_returns
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2025, 7, 1, 8, 0, tzinfo=UTC))
SEED_START, SEED_END = date(2024, 4, 1), date(2025, 6, 30)
T = date(2025, 5, 30)               # the probed signal session (a month end)


def _instrument(s, sym, itype="stock"):
    return s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        "instrument_type, name, currency, is_active) "
        "VALUES (:s, 'XTEST', 'US', :t, :s, 'USD', true) RETURNING id"),
        {"s": sym, "t": itype}).scalar()


def _seed_bars(s, iid, dates, price):
    s.execute(text(
        "INSERT INTO market.price_bars_daily (instrument_id, bar_date, open, "
        "high, low, close, volume, source) "
        "VALUES (:iid, :d, :c, :c, :c, :c, 1000, 'EodhdAdapter')"),
        [{"iid": iid, "d": d, "c": price(i)} for i, d in enumerate(dates)])


# ----------------------------------------------------------------- momentum

@pytest.fixture
def momentum_panel(pg_session):
    """Four deterministic series over the real US calendar: a clean riser, a
    2:1 split mid-window, a one-bar gap (must be excluded), and an unseasoned
    late listing (must be excluded)."""
    s = pg_session
    cal = trading_days_between("US", SEED_START, SEED_END)
    a = _instrument(s, "MOMA")
    _seed_bars(s, a, cal, lambda i: Decimal("100") + Decimal("0.25") * i)
    b = _instrument(s, "MOMB")
    _seed_bars(s, b, cal, lambda i: Decimal("50") + Decimal("0.10") * i)
    s.execute(text(
        "INSERT INTO market.corporate_actions (instrument_id, action_date, "
        "action_type, ratio, source) "
        "VALUES (:iid, '2025-02-18', 'split', 2, 'test')"), {"iid": b})
    c = _instrument(s, "MOMC")
    _seed_bars(s, c, [d for d in cal if d != date(2025, 1, 15)],
               lambda i: Decimal("30") + Decimal("0.05") * i)
    d = _instrument(s, "MOMD")
    _seed_bars(s, d, cal[-100:], lambda i: Decimal("20") + Decimal("0.05") * i)
    return s


def test_momentum_feature_equals_production_ranker_byte_identical(momentum_panel):
    s = momentum_panel
    formation, _ = _formation_returns(s, T)
    # the ranker's own verdicts on this panel, pinned (golden):
    assert formation == {"MOMA": 0.5249999999999999,
                         "MOMB": 1.8555555555555552}
    rep = materialize(s, MOMENTUM_12_1, clock=CLOCK,
                      symbols=["MOMA", "MOMB", "MOMC", "MOMD"], sessions=[T])
    assert rep.failed == ()
    for sym, expected in formation.items():
        got = feature_at(s, MOMENTUM_12_1, sym, on=T,
                         dataset_version=rep.dataset_version)
        assert got == expected, f"{sym}: store {got!r} != ranker {expected!r}"
    # exclusions agree too: a gappy or unseasoned series is scored by NEITHER
    for sym in ("MOMC", "MOMD"):
        assert sym not in formation
        assert feature_at(s, MOMENTUM_12_1, sym, on=T,
                          dataset_version=rep.dataset_version) is None
    assert rep.computed == {"MOMA": 1, "MOMB": 1, "MOMC": 0, "MOMD": 0}


def test_momentum_dense_sessions_pinned(momentum_panel):
    """Adjacent sessions carry DIFFERENT formation returns (each session is
    its own point-in-time fact) — pinned to the literals the fixture math
    produced when the pin was cut."""
    s = momentum_panel
    s1, s2 = date(2025, 5, 28), date(2025, 5, 29)
    rep = materialize(s, MOMENTUM_12_1, clock=CLOCK, symbols=["MOMA"],
                      sessions=[s1, s2, T])
    dv = rep.dataset_version
    assert feature_at(s, MOMENTUM_12_1, "MOMA", on=s1,
                      dataset_version=dv) == 0.5273972602739727
    assert feature_at(s, MOMENTUM_12_1, "MOMA", on=s2,
                      dataset_version=dv) == 0.5261958997722096
    assert feature_at(s, MOMENTUM_12_1, "MOMA", on=T,
                      dataset_version=dv) == 0.5249999999999999
    # weekend read carries Friday's value; the next session refuses stale
    assert feature_at(s, MOMENTUM_12_1, "MOMA", on=date(2025, 5, 31),
                      dataset_version=dv) == 0.5249999999999999
    assert feature_at(s, MOMENTUM_12_1, "MOMA", on=date(2025, 6, 2),
                      dataset_version=dv) is None


# ---------------------------------------------------------------------- SUE

def _quarters(first: date, n: int) -> list[date]:
    out, y, m = [], first.year, first.month
    days = {3: 31, 6: 30, 9: 30, 12: 31}
    for _ in range(n):
        out.append(date(y, m, days[m]))
        m += 3
        if m > 12:
            m, y = m - 12, y + 1
    return out


def _seed_reports(s, iid, sym, fpes, rds, surprises, whens):
    rows = []
    for fpe, rd, surp, when in zip(fpes, rds, surprises, whens):
        rows.append(EarningsSurprise(
            symbol=sym, fiscal_period_end=fpe, report_date=rd,
            eps_actual=Decimal("1.00") + Decimal(surp),
            eps_estimate=Decimal("1.00"), surprise_pct=None,
            before_after_market=when, currency=None))
        s.execute(text(
            "INSERT INTO market.earnings_surprises (instrument_id, "
            "fiscal_period_end, report_date, eps_actual, eps_estimate, "
            "surprise_pct, currency, before_after_market, source, fetched_at) "
            "VALUES (:iid, :fpe, :rd, :a, '1.00', NULL, 'USD', :w, 'test', :fa)"),
            {"iid": iid, "fpe": fpe, "rd": rd, "a": str(rows[-1].eps_actual),
             "w": when, "fa": CLOCK.now()})
    return rows


@pytest.fixture
def sue_panel(pg_session):
    """Two adversarial series. SUEA: alternating Before/AfterMarket flags, a
    LATE final report (rd 2025-06-10) that opens a staleness gap after report
    8 expires (~2025-05-19). SUEB: four varied surprises then nine identical
    ones — from report 11 (rd 2025-02-14) the prior window's stdev is ZERO,
    so SUE is UNDEFINED and must SHADOW the still-fresh report 10 into None
    (live()'s no-fallback rule: the dense representation's sharpest edge)."""
    s = pg_session
    a = _instrument(s, "SUEA")
    fpes_a = _quarters(date(2022, 12, 31), 10)
    rds_a = [f + timedelta(days=45) for f in fpes_a[:9]] + [date(2025, 6, 10)]
    whens_a = ["BeforeMarket" if i % 2 == 0 else "AfterMarket"
               for i in range(10)]
    reports_a = _seed_reports(
        s, a, "SUEA", fpes_a, rds_a,
        ("0.10", "0.20", "-0.10", "0.05", "0.15",
         "0.30", "-0.05", "0.10", "0.20", "0.40"), whens_a)
    b = _instrument(s, "SUEB")
    fpes_b = _quarters(date(2022, 3, 31), 13)
    reports_b = _seed_reports(
        s, b, "SUEB", fpes_b, [f + timedelta(days=45) for f in fpes_b],
        ("0.10", "0.20", "-0.10", "0.05") + ("0.05",) * 9,
        ["BeforeMarket"] * 13)
    return s, {"SUEA": reports_a, "SUEB": reports_b}


MSTART, MEND = date(2025, 1, 2), date(2025, 6, 30)


def test_sue_feature_equals_pead_live_dense(sue_panel):
    """feature_at == EarningsView.live() at EVERY materialized session, with
    the view built over a DIFFERENT (longer) calendar than the store used —
    live() depends only on session-index DIFFERENCES, and the store must
    reproduce it including staleness expiry and the undefined-SUE shadow."""
    s, reports = sue_panel
    sessions = trading_days_between("US", MSTART, MEND)
    rep = materialize(s, SUE_FOS, clock=CLOCK, symbols=["SUEA", "SUEB"],
                      sessions=sessions)
    assert rep.failed == ()
    cal = trading_days_between("US", date(2023, 6, 1), MEND)   # independent
    view = build_earnings_view(reports, cal)
    idx = {d: i for i, d in enumerate(cal)}
    checked_none = checked_val = 0
    for sym in ("SUEA", "SUEB"):
        for t in sessions:
            expected = view.live(sym, idx[t], variant="sue")
            got = feature_at(s, SUE_FOS, sym, on=t,
                             dataset_version=rep.dataset_version)
            assert got == expected, (f"{sym}@{t}: store {got!r} != "
                                     f"live {expected!r}")
            if expected is None:
                checked_none += 1
            else:
                checked_val += 1
    assert checked_val and checked_none          # both regimes exercised
    assert rep.computed == {"SUEA": 106, "SUEB": 29}


def test_sue_feature_equals_production_ranker_at_signal_session(sue_panel):
    """Full production path: _live_sues (tradable universe + live SUE) at a
    signal session must match the store value for value; SUEB is shadowed by
    its undefined report 11 and must appear in NEITHER."""
    s, _ = sue_panel
    t_sig = date(2025, 3, 3)
    for sym in ("SUEA", "SUEB"):
        iid = s.execute(text(
            "SELECT id FROM market.instruments WHERE symbol = :s"),
            {"s": sym}).scalar()
        s.execute(text(
            "INSERT INTO market.price_bars_daily (instrument_id, bar_date, "
            "open, high, low, close, volume, source) "
            "VALUES (:iid, :d, 10, 10, 10, 10, 100, 'EodhdAdapter')"),
            {"iid": iid, "d": t_sig})
    rep = materialize(s, SUE_FOS, clock=CLOCK, symbols=["SUEA", "SUEB"],
                      sessions=[t_sig])
    sues, _ = _live_sues(s, t_sig)
    assert sues == {"SUEA": 1.5457468529268736}          # golden pin
    assert feature_at(s, SUE_FOS, "SUEA", on=t_sig,
                      dataset_version=rep.dataset_version) == sues["SUEA"]
    assert feature_at(s, SUE_FOS, "SUEB", on=t_sig,
                      dataset_version=rep.dataset_version) is None


def test_sue_regimes_pinned(sue_panel):
    """The dense series' regime boundaries, pinned to golden literals:
    report handover, staleness expiry (gap -> None), late-report revival,
    and the undefined-SUE shadow."""
    s, _ = sue_panel
    sessions = trading_days_between("US", MSTART, MEND)
    dv = materialize(s, SUE_FOS, clock=CLOCK, symbols=["SUEA", "SUEB"],
                     sessions=sessions).dataset_version

    def at(sym, on):
        return feature_at(s, SUE_FOS, sym, on=on, dataset_version=dv)

    # SUEA: report 7 era -> report 8 handover (BeforeMarket 2025-02-14)
    assert at("SUEA", date(2025, 1, 15)) == 0.7156780854205468
    assert at("SUEA", date(2025, 2, 13)) == 0.7156780854205468
    assert at("SUEA", date(2025, 2, 14)) == 1.5457468529268736
    # report 8 goes stale after 63 sessions (~2025-05-19): honest gap
    assert at("SUEA", date(2025, 5, 20)) is None
    assert at("SUEA", date(2025, 6, 10)) is None   # AfterMarket print day
    # ... tradable the NEXT session (AfterMarket -> bisect_right)
    assert at("SUEA", date(2025, 6, 11)) == 2.967473134823095
    assert at("SUEA", date(2025, 6, 30)) == 2.967473134823095
    # SUEB: defined until report 11 lands undefined (zero-stdev priors) and
    # SHADOWS the still-fresh report 10 — no fallback, exactly as live()
    assert at("SUEB", date(2025, 2, 13)) == 0.9428090415820634
    assert at("SUEB", date(2025, 2, 14)) is None
    assert at("SUEB", date(2025, 5, 30)) is None
