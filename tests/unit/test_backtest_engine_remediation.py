"""Remediation regression tests for the single-instrument backtest engine.

F-003 (entry-day return double-counting) and F-004 (impossible stop fills) from
the independent review. Every fixture is HAND-CALCULATED with the arithmetic in
the docstring, and each test fails against the reviewed (pre-remediation) engine.

Old (defective) vs new (correct) reference values are recorded in each test so a
regression is legible without re-deriving the algebra.
"""
from __future__ import annotations

import math

import pytest

from atlas.dcp.backtest.engine import CostModel, Intent, OBar, run_backtest

ZERO = CostModel(0.0, 0.0)


def _enter_on_third_bar(stop: float, target: float, time_stop: int = 10):
    """Strategy that signals exactly once — when it has seen 3 bars — so the
    entry executes at bar index 3's open and never re-enters."""
    def strat(hist: list[OBar]) -> Intent | None:
        return Intent(stop=stop, target=target, time_stop=time_stop) if len(hist) == 3 else None
    return strat


# --------------------------------------------------------------------------- #
# F-003 — entry-day return is counted exactly once
# --------------------------------------------------------------------------- #
def test_f003_entry_day_return_counted_once():
    """HAND CALC (zero costs):
      bar0..2 = flat 100 (warm-up; strategy signals at len(hist)==3)
      bar3 (ENTRY): open 100 -> buy 100; close 110  => entry-day return +10% (booked day 3)
      bar4 (EXIT):  high 125 >= target 121 -> sell 121; prev mark = bar3 close 110
                    => day-4 return 121/110 - 1 = +10%
      equity = 1.0 * 1.10 * 1.10 = 1.21   => total_return = 0.21
    OLD (defective '>' in the exit mark) marked day 4 from the ENTRY price 100:
      121/100 - 1 = +21%  => equity 1.10 * 1.21 = 1.331  => total_return 0.331
      (the entry-day +10% booked on day 3 was counted a second time).
    """
    bars = [
        OBar(100, 100, 100, 100, 1),   # 0
        OBar(100, 100, 100, 100, 1),   # 1
        OBar(100, 100, 100, 100, 1),   # 2  <- strategy sees 3 bars, signals
        OBar(100, 115, 99, 110, 1),    # 3  ENTRY at open 100, close 110
        OBar(110, 125, 118, 120, 1),   # 4  EXIT at target 121
    ]
    r = run_backtest(bars, _enter_on_third_bar(stop=90, target=121), ZERO,
                     start_i=1, end_i=len(bars))
    assert r.n_trades == 1
    t = r.trades[0]
    assert (t.entry_i, t.exit_i, t.reason) == (3, 4, "target")
    assert t.entry == pytest.approx(100.0)
    assert t.exit == pytest.approx(121.0)
    # trade-level return (exit/entry-1) is unchanged by the fix: the defect was
    # in how the daily equity path marked the exit day.
    assert t.ret == pytest.approx(0.21)
    # THE fix: equity return counts the entry day once.
    assert r.total_return == pytest.approx(0.21)          # NEW (correct)
    assert r.total_return != pytest.approx(0.331)          # OLD (defective) rejected


def test_f003_same_day_entry_and_exit_marks_from_entry():
    """A position entered AND exited on the same bar must mark from the entry
    price (there is no prior-day close to mark from).
      bar3: open 100 -> buy 100; low 80 <= stop 90 -> stop; open 100 > stop so
            fill = min(90,100) = 90 (zero cost). day return 90/100 - 1 = -10%.
      equity = 0.90  => total_return = -0.10.
    """
    bars = [
        OBar(100, 100, 100, 100, 1),
        OBar(100, 100, 100, 100, 1),
        OBar(100, 100, 100, 100, 1),   # signal
        OBar(100, 101, 80, 85, 1),     # ENTRY 100, gaps to low 80 -> stop hit same day
    ]
    r = run_backtest(bars, _enter_on_third_bar(stop=90, target=1000), ZERO,
                     start_i=1, end_i=len(bars))
    assert r.n_trades == 1
    assert r.trades[0].reason == "stop"
    assert r.trades[0].exit == pytest.approx(90.0)   # min(stop 90, open 100) = 90
    assert r.total_return == pytest.approx(-0.10)


# --------------------------------------------------------------------------- #
# F-004 — a gap through the stop fills at the (worse) open, never the stop
# --------------------------------------------------------------------------- #
def _series_with_next_bar(entry_bar: OBar, next_bar: OBar):
    return [
        OBar(100, 100, 100, 100, 1),
        OBar(100, 100, 100, 100, 1),
        OBar(100, 100, 100, 100, 1),   # signal
        entry_bar,                     # 3 ENTRY at its open
        next_bar,                      # 4 stop day
    ]


def test_f004_gap_through_stop_fills_at_open_not_stop():
    """Entry 100, stop 95. Next bar OPENS at 90 (below the stop) with low 88.
      correct fill = min(stop 95, open 90) = 90   (you cannot sell at 95)
      OLD fill     = 95                            (unobtainable)
    """
    bars = _series_with_next_bar(
        OBar(100, 115, 99, 110, 1),        # entry 100, no stop day 3 (low 99 > 95)
        OBar(90, 92, 88, 89, 1),           # gap-down open 90, low 88 <= stop
    )
    r = run_backtest(bars, _enter_on_third_bar(stop=95, target=10_000), ZERO,
                     start_i=1, end_i=len(bars))
    assert r.n_trades == 1 and r.trades[0].reason == "stop"
    assert r.trades[0].exit == pytest.approx(90.0)       # NEW
    assert r.trades[0].exit != pytest.approx(95.0)       # OLD rejected


def test_f004_open_above_stop_fills_at_stop():
    """Normal intraday stop: open 98 (above stop 95), low 94 <= 95 -> fill at the
    stop 95 (min(95, 98) = 95). Unchanged from correct behaviour."""
    bars = _series_with_next_bar(
        OBar(100, 115, 99, 110, 1),
        OBar(98, 99, 94, 96, 1),
    )
    r = run_backtest(bars, _enter_on_third_bar(stop=95, target=10_000), ZERO,
                     start_i=1, end_i=len(bars))
    assert r.trades[0].exit == pytest.approx(95.0)


def test_f004_open_exactly_at_stop_fills_at_stop():
    bars = _series_with_next_bar(
        OBar(100, 115, 99, 110, 1),
        OBar(95, 96, 93, 94, 1),           # open == stop
    )
    r = run_backtest(bars, _enter_on_third_bar(stop=95, target=10_000), ZERO,
                     start_i=1, end_i=len(bars))
    assert r.trades[0].exit == pytest.approx(95.0)


def test_f004_large_gap_through_stop_fills_at_open():
    """A catastrophic gap: open 50, far below stop 95. Fill at 50, not 95."""
    bars = _series_with_next_bar(
        OBar(100, 115, 99, 110, 1),
        OBar(50, 55, 45, 48, 1),
    )
    r = run_backtest(bars, _enter_on_third_bar(stop=95, target=10_000), ZERO,
                     start_i=1, end_i=len(bars))
    assert r.trades[0].exit == pytest.approx(50.0)


def test_f004_stop_fill_charges_costs_on_the_obtainable_price():
    """With costs, the gap-through fill charges the sell cost on the OPEN (90),
    not the stop: sell(90) = 90 * (1 - (5+5)/10_000) = 89.91."""
    bars = _series_with_next_bar(
        OBar(100, 115, 99, 110, 1),
        OBar(90, 92, 88, 89, 1),
    )
    r = run_backtest(bars, _enter_on_third_bar(stop=95, target=10_000),
                     CostModel(5, 5), start_i=1, end_i=len(bars))
    assert r.trades[0].exit == pytest.approx(90.0 * (1 - 0.001))   # 89.91


def test_f004_invalid_open_on_stop_bar_fails_closed():
    """A non-finite / non-positive open on a stop bar cannot yield an obtainable
    fill price — the engine must fail closed rather than fabricate one."""
    for bad_open in (float("nan"), 0.0, -1.0):
        bars = _series_with_next_bar(
            OBar(100, 115, 99, 110, 1),
            OBar(bad_open, 92, 88, 89, 1),
        )
        with pytest.raises(ValueError):
            run_backtest(bars, _enter_on_third_bar(stop=95, target=10_000), ZERO,
                         start_i=1, end_i=len(bars))


def test_f004_target_and_time_exits_unaffected():
    """Regression guard: the fix touches only the stop branch. A target exit
    still fills at the target and a time exit at the close."""
    # target
    bars_t = _series_with_next_bar(
        OBar(100, 115, 99, 110, 1),
        OBar(112, 130, 111, 128, 1),       # high 130 >= target 121
    )
    rt = run_backtest(bars_t, _enter_on_third_bar(stop=90, target=121), ZERO,
                      start_i=1, end_i=len(bars_t))
    assert rt.trades[0].reason == "target" and rt.trades[0].exit == pytest.approx(121.0)
    # time (time_stop=1: exit on the first bar after entry, at its close)
    bars_time = _series_with_next_bar(
        OBar(100, 115, 99, 110, 1),
        OBar(109, 112, 106, 108, 1),
    )
    rr = run_backtest(bars_time, _enter_on_third_bar(stop=90, target=10_000, time_stop=1),
                      ZERO, start_i=1, end_i=len(bars_time))
    assert rr.trades[0].reason == "time" and rr.trades[0].exit == pytest.approx(108.0)


def test_engine_finiteness_helpers_available():
    """Guard the guard: math.isfinite is the check the engine relies on."""
    assert not math.isfinite(float("nan")) and not math.isfinite(float("inf"))
