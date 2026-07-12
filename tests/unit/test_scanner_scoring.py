"""Scanner v1 scoring math (atlas/dcp/scanner/v1.py, ADR-0007).

Golden pins on a hand-verified cross-section. Remember what these numbers
ARE: attention scores, not alpha — the pins freeze the RANKING RULES (so a
drive-by 'improvement' cannot silently reroute desk attention), they do not
bless the rules as predictive. Changing a pin here is changing strategy
surface and belongs in a reviewed criteria-version bump.
"""
from __future__ import annotations

import pytest

from atlas.dcp.scanner.v1 import (LOOKBACK_SESSIONS, SymbolScore, rank01,
                                  score_cross_section, volume_surge)

# Hand-built cross-section (60 sessions each; only the LAST close moves so the
# 20-session return is just |last/100 - 1|):
#   AAA: last close 150 -> ret .5 ; last-5 volume 3000 vs 55x1000 -> surge 18/7
#   BBB: last close 110 -> ret .1 ; last-5 volume 5000            -> surge 3.75
#   CCC: last close  60 -> ret .4 (DOWN moves draw attention too) ; flat volume
#   DDD: flat            -> ret .0 ; flat volume
# ret ranks   (asc, /3): DDD 0, BBB 1/3, CCC 2/3, AAA 1
# surge ranks (asc, /3): CCC 0, DDD 1/3 (tie 1.0 broken by symbol), AAA 2/3, BBB 1
# scores: AAA 5/3, BBB 4/3, CCC 2/3, DDD 1/3


def _series(last_close: float, surge_vol: int | None = None,
            n: int = LOOKBACK_SESSIONS) -> tuple[list[float], list[int]]:
    closes = [100.0] * (n - 1) + [last_close]
    volumes = [1000] * n if surge_vol is None else [1000] * (n - 5) + [surge_vol] * 5
    return closes, volumes


GOLDEN = {
    "AAA": _series(150.0, surge_vol=3000),
    "BBB": _series(110.0, surge_vol=5000),
    "CCC": _series(60.0),
    "DDD": _series(100.0),
}


def test_volume_surge_math():
    _, volumes = _series(100.0, surge_vol=3000)
    base_mean = (55 * 1000 + 5 * 3000) / 60
    assert volume_surge(volumes) == pytest.approx(3000 / base_mean)  # 18/7
    assert volume_surge([1000] * 60) == pytest.approx(1.0)


def test_volume_surge_uses_only_the_last_60_sessions():
    _, volumes = _series(100.0, surge_vol=3000)
    with_noise = [10**9] * 10 + volumes  # older bars must not touch the baseline
    assert volume_surge(with_noise) == volume_surge(volumes)


def test_volume_surge_dead_tape_is_zero_not_a_crash():
    assert volume_surge([0] * 60) == 0.0


def test_volume_surge_refuses_thin_series():
    with pytest.raises(ValueError, match="needs >= 60 volumes"):
        volume_surge([1000] * 59)


def test_rank01_ties_break_by_symbol():
    ranks = rank01({"B": 1.0, "A": 1.0, "C": 2.0})
    assert ranks == {"A": 0.0, "B": 0.5, "C": 1.0}


def test_rank01_single_name_cross_section():
    assert rank01({"X": 5.0}) == {"X": 0.0}


def test_golden_cross_section_pins():
    scores = score_cross_section(GOLDEN)
    assert [sc.symbol for sc in scores] == ["AAA", "BBB", "CCC", "DDD"]
    aaa, bbb, ccc, ddd = scores
    assert aaa.ret20_abs == pytest.approx(0.5)
    assert aaa.ret20_rank == pytest.approx(1.0)
    assert aaa.volume_surge == pytest.approx(18 / 7)
    assert aaa.surge_rank == pytest.approx(2 / 3)
    assert aaa.score == pytest.approx(5 / 3)
    assert bbb.ret20_abs == pytest.approx(0.1)
    assert bbb.ret20_rank == pytest.approx(1 / 3)
    assert bbb.volume_surge == pytest.approx(3.75)
    assert bbb.surge_rank == pytest.approx(1.0)
    assert bbb.score == pytest.approx(4 / 3)
    # a 40% DECLINE outranks a 10% rally on the return component: |.| is the point
    assert ccc.ret20_abs == pytest.approx(0.4)
    assert ccc.ret20_rank == pytest.approx(2 / 3)
    assert ccc.surge_rank == pytest.approx(0.0)  # 1.0 surge tie: CCC before DDD
    assert ccc.score == pytest.approx(2 / 3)
    assert ddd.ret20_abs == pytest.approx(0.0)
    assert ddd.surge_rank == pytest.approx(1 / 3)
    assert ddd.score == pytest.approx(1 / 3)


def test_determinism_input_order_never_matters():
    forward = score_cross_section(GOLDEN)
    backward = score_cross_section(dict(reversed(list(GOLDEN.items()))))
    assert forward == backward
    assert score_cross_section(GOLDEN) == forward  # and re-runs are identical


def test_final_score_ties_break_by_symbol():
    # ret ranks ascend A<B<C<D while surge ranks descend A>B>C>D: every total
    # score is exactly 1.0, so the ORDER is decided by symbol alone — total,
    # deterministic, and independent of dict insertion order
    tied = {
        "DDD": _series(130.0),                  # ret rank 1.0, surge rank 0.0
        "CCC": _series(120.0, surge_vol=2000),  # ret rank 2/3, surge rank 1/3
        "BBB": _series(110.0, surge_vol=3000),  # ret rank 1/3, surge rank 2/3
        "AAA": _series(100.0, surge_vol=5000),  # ret rank 0.0, surge rank 1.0
    }
    scores = score_cross_section(tied)
    assert [sc.symbol for sc in scores] == ["AAA", "BBB", "CCC", "DDD"]
    assert isinstance(scores[0], SymbolScore)
    assert all(sc.score == pytest.approx(1.0) for sc in scores)


def test_fail_closed_guards():
    with pytest.raises(ValueError, match="needs >= 21 closes"):
        score_cross_section({"SHRT": ([100.0] * 20, [1000] * 60)})
    with pytest.raises(ValueError, match="non-positive close"):
        closes = [100.0] * 60
        closes[-21] = 0.0
        score_cross_section({"ZERO": (closes, [1000] * 60)})
