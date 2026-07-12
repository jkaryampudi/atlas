"""fxlab engine (ADR-0008): hand-pinned cost arithmetic on tiny series —
spread legs on entry/exit/flip, swap accrual per night held, short profit
math verified by hand in comments — plus the structural no-look-ahead
property (future perturbation, mirrors test_backtest_engine.py)."""
import random
from datetime import date, timedelta

import pytest

from atlas.fxlab.engine import (SPREAD_PER_SIDE, SWAP_PER_NIGHT, FxBar,
                                run_fx_backtest, run_fx_positions)

S, W = SPREAD_PER_SIDE, SWAP_PER_NIGHT
D0 = date(2024, 1, 1)


def fb(o: float, h: float, lo: float, c: float, day: int) -> FxBar:
    return FxBar(bar_date=D0 + timedelta(days=day), open=o, high=h, low=lo, close=c)


def flat(c: float, day: int) -> FxBar:
    return fb(c, c, c, c, day)


def test_bar_has_no_volume_field():
    """EODHD FOREX volume is untrustworthy; it must not exist to be leaned on."""
    assert not hasattr(flat(1.0, 0), "volume")


def test_long_entry_hold_exit_hand_pinned():
    """positions [1, 1, 0] over bars b1..b3, start_i=1.

    day1 (b1, prev=0 -> pos=1): overnight 0; session 1.01/1.00-1 = +1.00%;
      one entry leg -S; no swap (was flat overnight).
    day2 (b2, held): overnight 1.02/1.01-1; session 1.03/1.02-1; no legs;
      one night held -> -W.
    day3 (b3, exit at open): overnight 1.03/1.03-1 = 0; flat session 0;
      one exit leg -S; the night into day3 was held -> -W.
    Flat at the end -> no liquidation adjustment. One entry -> n_trades=1."""
    bars = [fb(1.00, 1.00, 1.00, 1.00, 0),
            fb(1.00, 1.01, 1.00, 1.01, 1),
            fb(1.02, 1.03, 1.02, 1.03, 2),
            fb(1.03, 1.03, 1.02, 1.02, 3)]
    r = run_fx_positions(bars, [1, 1, 0], start_i=1)
    r1 = (1.01 / 1.00 - 1) - S
    r2 = (1.02 / 1.01 - 1) + (1.03 / 1.02 - 1) - W
    r3 = 0.0 - S - W
    assert r.total_return == pytest.approx((1 + r1) * (1 + r2) * (1 + r3) - 1)
    assert r.n_trades == 1
    assert (r.exposure_long, r.exposure_short, r.exposure_flat) == \
        pytest.approx((2 / 3, 0.0, 1 / 3))
    assert r.equity[0] == 1.0 and len(r.equity) == 4


def test_flip_costs_two_legs_and_final_liquidation_hand_pinned():
    """Flat prices isolate pure costs. positions [1, -1]:
    day1: entry leg -S. day2: flip +1 -> -1 = TWO legs -2S, plus one night
    held -W. Still short at the end -> forced liquidation at the final close
    pays one more leg, folded into the last day: equity = (1-S)(1-2S-W)(1-S)."""
    bars = [flat(1.0, 0), flat(1.0, 1), flat(1.0, 2)]
    r = run_fx_positions(bars, [1, -1], start_i=1)
    assert r.total_return == pytest.approx((1 - S) * (1 - 2 * S - W) * (1 - S) - 1)
    assert r.n_trades == 2  # entry, then the flip counts as a new entry


def test_short_profit_math_hand_pinned():
    """Short 1 unit at the open 1.00, price closes at 0.99: the short GAINS
    +1.00% on the session (-1 * (0.99/1.00 - 1)), pays the entry leg -S, and
    the end-of-window cover pays the exit leg: equity = (1 + 0.01 - S)(1 - S).
    Net is positive — shorts profit when the pair falls."""
    bars = [flat(1.00, 0), fb(1.00, 1.00, 0.99, 0.99, 1)]
    r = run_fx_positions(bars, [-1], start_i=1)
    assert r.total_return == pytest.approx((1 + 0.01 - S) * (1 - S) - 1)
    assert r.total_return > 0
    assert (r.exposure_long, r.exposure_short, r.exposure_flat) == (0.0, 1.0, 0.0)


def test_swap_accrues_per_night_held_hand_pinned():
    """Long 4 sessions on flat prices: entry leg on day1, then exactly THREE
    held nights (into days 2, 3, 4) at -W each, exit leg at liquidation:
    equity = (1-S) * (1-W)^3 * (1-S). Direction is irrelevant: a short pays
    the identical swap (conservative symmetric approximation, ADR-0008 §4)."""
    bars = [flat(1.0, d) for d in range(5)]
    long_r = run_fx_positions(bars, [1, 1, 1, 1], start_i=1)
    short_r = run_fx_positions(bars, [-1, -1, -1, -1], start_i=1)
    expected = (1 - S) ** 2 * (1 - W) ** 3 - 1
    assert long_r.total_return == pytest.approx(expected)
    assert short_r.total_return == pytest.approx(expected)


def test_overnight_gap_belongs_to_the_overnight_holder():
    """A gap up at the open accrues to yesterday's position, not today's:
    positions [1, 0] with a +2% overnight gap into day2 — day2 is flat at the
    open but the engine exits AT the open, so the holder keeps the gap:
    day2 r = 1*(1.02/1.00 - 1) + 0 - S - W."""
    bars = [flat(1.00, 0), flat(1.00, 1), fb(1.02, 1.02, 1.02, 1.02, 2)]
    r = run_fx_positions(bars, [1, 0], start_i=1)
    r1 = 0.0 - S
    r2 = (1.02 / 1.00 - 1) - S - W
    assert r.total_return == pytest.approx((1 + r1) * (1 + r2) - 1)


def test_flat_positions_cost_nothing():
    bars = [flat(1.0 + 0.01 * d, d) for d in range(6)]
    r = run_fx_positions(bars, [0] * 5, start_i=1)
    assert r.total_return == 0.0
    assert r.sharpe == 0.0
    assert r.n_trades == 0
    assert r.exposure_flat == 1.0


def test_invalid_position_and_window_are_refused():
    bars = [flat(1.0, d) for d in range(4)]
    with pytest.raises(ValueError, match="position must be"):
        run_fx_positions(bars, [2], start_i=1)
    with pytest.raises(ValueError, match="start_i"):
        run_fx_positions(bars, [1], start_i=0)
    with pytest.raises(ValueError, match="past the end"):
        run_fx_positions(bars, [1, 1, 1, 1], start_i=1)
    with pytest.raises(ValueError, match="bad window"):
        run_fx_backtest(bars, lambda h: 0, start_i=0)


def _walk(n: int, seed: int = 3) -> list[FxBar]:
    rng = random.Random(seed)
    out, px = [], 1.10
    for d in range(n):
        o = px * (1 + rng.uniform(-0.002, 0.002))
        c = o * (1 + rng.uniform(-0.006, 0.006))
        hi, lo = max(o, c) * 1.001, min(o, c) * 0.999
        out.append(fb(o, hi, lo, c, d))
        px = c
    return out


def test_strategy_sees_only_history_before_the_session():
    """The position held through day t comes from strategy(bars[:t]) — the
    strategy never sees bar t itself, so today's close cannot leak into
    today's position."""
    bars = _walk(40)
    seen: list[int] = []

    def spy(hist: list[FxBar]) -> int:
        seen.append(len(hist))
        return 1 if hist[-1].close > hist[0].close else -1

    r = run_fx_backtest(bars, spy, start_i=5, end_i=30)
    assert seen == list(range(5, 30))          # exactly bars[:t] for each session t
    assert len(r.positions) == 25


def test_no_look_ahead_future_perturbation():
    """Corrupting every bar AFTER the cut must not change any position held
    on or before the cut (house property test)."""
    bars = _walk(120)
    cut = 80
    corrupted = bars[:cut] + [flat(9.99, d) for d in range(cut, len(bars))]
    a = run_fx_backtest(bars, lambda h: 1 if h[-1].close > h[-5].close else -1,
                        start_i=10, end_i=len(bars))
    b = run_fx_backtest(corrupted, lambda h: 1 if h[-1].close > h[-5].close else -1,
                        start_i=10, end_i=len(bars))
    # session t uses bars[:t]; sessions with t <= cut depend only on bars < cut
    assert a.positions[:cut - 10 + 1] == b.positions[:cut - 10 + 1]


def test_costs_only_ever_hurt():
    """Any traded path on flat prices strictly loses (spread+swap are pure
    drag); doing nothing is exactly zero — ADR-0008's benchmark."""
    bars = [flat(1.0, d) for d in range(10)]
    traded = run_fx_positions(bars, [1, 0, -1, -1, 0, 1, 1, 1, 0], start_i=1)
    nothing = run_fx_positions(bars, [0] * 9, start_i=1)
    assert traded.total_return < 0
    assert nothing.total_return == 0.0
