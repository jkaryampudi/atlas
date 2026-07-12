"""fxlab candidates (ADR-0008): hand-verified signal pins on constructed
series for all three textbook rules, warmup behaviour, and the structural
no-look-ahead property through the fxlab engine (house style, mirrors
test_signals_trend.py)."""
import random
from datetime import date, timedelta

from atlas.fxlab.candidates import (CANDIDATES, ENTRY_WINDOW, FAST, RSI_WINDOW, SLOW,
                                    WARMUP, donchian, ma_cross, rsi_fade)
from atlas.fxlab.engine import FxBar, run_fx_backtest

D0 = date(2024, 1, 1)


def fb(o: float, h: float, lo: float, c: float, day: int) -> FxBar:
    return FxBar(bar_date=D0 + timedelta(days=day), open=o, high=h, low=lo, close=c)


def flats(closes: list[float]) -> list[FxBar]:
    return [fb(c, c, c, c, d) for d, c in enumerate(closes)]


def test_specs_are_textbook_and_fxlab_family_named():
    for name, (_, spec) in CANDIDATES.items():
        assert spec["family"] == f"fxlab-{name}"
        assert "no search" in str(spec["provenance"])
    assert (FAST, SLOW, ENTRY_WINDOW, RSI_WINDOW) == (50, 200, 20, 14)
    assert WARMUP == SLOW  # longest lookback across candidates


# --- ma_cross ---------------------------------------------------------------

def test_ma_cross_warmup_and_tie_are_flat():
    assert ma_cross(flats([1.0] * (SLOW - 1))) == 0     # not enough history
    assert ma_cross(flats([1.0] * SLOW)) == 0           # SMA50 == SMA200 -> flat


def test_ma_cross_golden_and_death_cross_hand_pinned():
    """199 bars at 1.0 then one at 1.1: SMA50 = (49 + 1.1)/50 = 1.002 >
    SMA200 = (199 + 1.1)/200 = 1.0005 -> long. Mirrored down bar (0.9):
    SMA50 = 0.998 < SMA200 = 0.9995 -> short."""
    assert ma_cross(flats([1.0] * (SLOW - 1) + [1.1])) == 1
    assert ma_cross(flats([1.0] * (SLOW - 1) + [0.9])) == -1


def test_ma_cross_regime_persistence():
    """150 bars at 1.0 + 50 at 1.1: SMA50 = 1.1 > SMA200 = 1.025 -> long."""
    assert ma_cross(flats([1.0] * 150 + [1.1] * 50)) == 1
    assert ma_cross(flats([1.0] * 150 + [0.9] * 50)) == -1


# --- donchian ---------------------------------------------------------------

def test_donchian_warmup_is_flat():
    assert donchian(flats([1.0] * ENTRY_WINDOW)) == 0   # channel not yet full


def test_donchian_breakout_entries_hand_pinned():
    """25 flat bars at 1.0, then a close at 1.05: prior-20-bar high is 1.0,
    1.05 > 1.0 -> long. Mirrored 0.95 close breaks the prior 20-bar low ->
    short."""
    base = flats([1.0] * 25)
    assert donchian(base + [fb(1.0, 1.05, 1.0, 1.05, 25)]) == 1
    assert donchian(base + [fb(1.0, 1.0, 0.95, 0.95, 25)]) == -1


def test_donchian_ten_day_exit_hand_pinned():
    """After the long entry at 1.05 and 12 bars at 1.05, the prior-10-bar low
    is 1.05; a close at 1.04 < 1.05 exits to flat, and 1.04 stays above the
    prior-20-bar low (still 1.0 — the flat prefix is inside the entry
    channel), so no short entry follows."""
    bars = (flats([1.0] * 25) + [fb(1.0, 1.05, 1.0, 1.05, 25)]
            + [fb(1.05, 1.05, 1.05, 1.05, 26 + k) for k in range(12)]
            + [fb(1.05, 1.05, 1.04, 1.04, 38)])
    assert donchian(bars) == 0


def test_donchian_opposite_twenty_day_break_flips_same_bar():
    """Short from the 0.95 break; a later close at 1.06 clears BOTH the
    10-day exit channel (cover) and the prior-20-day high (1.0) -> the state
    machine exits and reverses long in the same session."""
    bars = (flats([1.0] * 25) + [fb(1.0, 1.0, 0.95, 0.95, 25)]
            + [fb(0.95, 1.06, 0.95, 1.06, 26)])
    assert donchian(bars) == 1


# --- rsi_fade ---------------------------------------------------------------

def _downtrend(n: int) -> list[float]:
    return [1.0 - 0.001 * k for k in range(n + 1)]  # n falls of 0.001


def test_rsi_fade_warmup_and_flat_are_neutral():
    assert rsi_fade(flats([1.0] * RSI_WINDOW)) == 0        # < 15 bars
    # totally flat window: no gains AND no losses -> RSI neutral (50), never
    # a signal (the documented 0/0 convention)
    assert rsi_fade(flats([1.0] * 40)) == 0


def test_rsi_fade_oversold_long_overbought_short():
    """14 straight falls -> avg gain 0, RSI = 0 < 30 -> long (fade). The
    mirrored uptrend -> RSI = 100 > 70 -> short."""
    assert rsi_fade(flats(_downtrend(RSI_WINDOW))) == 1
    assert rsi_fade(flats([1.0 + 0.001 * k for k in range(RSI_WINDOW + 1)])) == -1


def test_rsi_fade_midline_exit_hand_pinned():
    """15 falls of 0.001 (long, RSI=0), then +0.02: Wilder update gives
    avg_gain = 0.02/14 = 0.0014286, avg_loss = 0.001*13/14 = 0.00092857,
    RS = 1.53846, RSI = 100 - 100/2.53846 = 60.6 — crosses the 50 midline
    (exit long) but stays under 70, so the book is flat."""
    closes = _downtrend(15)
    closes.append(closes[-1] + 0.02)
    assert rsi_fade(flats(closes)) == 0


def test_rsi_fade_violent_reversal_can_flip_same_bar():
    """Same construction with +0.05: avg_gain = 0.0035714, RSI = 79.4 —
    the midline exit and the >70 short entry land on the same bar (exit is
    evaluated first, then entry; documented behaviour)."""
    closes = _downtrend(15)
    closes.append(closes[-1] + 0.05)
    assert rsi_fade(flats(closes)) == -1


# --- shared properties -------------------------------------------------------

def _walk(n: int, seed: int = 11) -> list[FxBar]:
    rng = random.Random(seed)
    out, px = [], 1.10
    for d in range(n):
        o = px * (1 + rng.uniform(-0.002, 0.002))
        c = o * (1 + rng.uniform(-0.006, 0.006))
        out.append(fb(o, max(o, c) * 1.001, min(o, c) * 0.999, c, d))
        px = c
    return out


def test_all_candidates_no_look_ahead_future_perturbation():
    """Corrupting bars after the cut changes no position on or before it."""
    bars = _walk(WARMUP + 60)
    cut = WARMUP + 40
    corrupted = bars[:cut] + [fb(9.9, 9.9, 9.9, 9.9, d)
                              for d in range(cut, len(bars))]
    for name, (strat, _) in CANDIDATES.items():
        a = run_fx_backtest(bars, strat, start_i=WARMUP + 1, end_i=len(bars))
        b = run_fx_backtest(corrupted, strat, start_i=WARMUP + 1, end_i=len(bars))
        keep = cut - (WARMUP + 1) + 1
        assert a.positions[:keep] == b.positions[:keep], name


def test_all_candidates_emit_only_valid_positions():
    bars = _walk(WARMUP + 30)
    for name, (strat, _) in CANDIDATES.items():
        r = run_fx_backtest(bars, strat, start_i=WARMUP, end_i=len(bars))
        assert set(r.positions) <= {-1, 0, 1}, name
