"""Latched breaker fold (Doc 04 §5, review finding): DD2/DD3 must survive NAV
recovery — only the dual-confirmed human clearance may step them down. The
stateless computed_breaker view of the same history would clear on recovery;
the fold must not. A clearance instant makes the first point at or after it
evaluate with human_cleared=True, landing on the COMPUTED target — so a
clearance during a still-deep drawdown changes nothing (you clear a latched
memory of a drawdown, never a live one), and with zero clearances the fold is
byte-identical to the pre-clearance latch."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from atlas.dcp.risk.engine import BreakerLevel, computed_breaker, drawdown
from atlas.dcp.trading.proposals import _breaker_fold

T0 = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)


def _at(i: int) -> datetime:
    return T0 + timedelta(days=i)


def _pts(*navs: int) -> list[tuple[datetime, Decimal]]:
    """Daily (as_of, nav) points — the fold's trading.portfolio_snapshots shape."""
    return [(_at(i), Decimal(n)) for i, n in enumerate(navs)]


# ------------------------------------------------- the latch (no clearances)

def test_dd2_latches_through_recovery():
    pts = _pts(100_000, 87_000, 95_000)  # -13% then -5%
    assert _breaker_fold(pts) is BreakerLevel.DD2
    # the stateless view of the final point alone says DD1 — the latch must win
    assert computed_breaker(drawdown(Decimal(95_000), Decimal(100_000))) \
        is BreakerLevel.DD1


def test_dd1_tracks_drawdown_without_latching():
    pts = _pts(100_000, 91_000, 99_000)  # -9% then -1%
    assert _breaker_fold(pts) is BreakerLevel.NONE


def test_latched_dd2_still_escalates_to_dd3():
    pts = _pts(100_000, 87_000, 95_000, 84_000)
    assert _breaker_fold(pts) is BreakerLevel.DD3  # -16% overrides the DD2 latch


def test_empty_history_is_calm():
    assert _breaker_fold(_pts(100_000)) is BreakerLevel.NONE


# --------------------------------------- dual-confirmed clearances (Doc 04 §5)

def test_clearance_between_points_steps_down_to_computed_target():
    # DD2 latched at 87k; cleared before the 95k point -> that step evaluates
    # human_cleared=True and lands on the computed target for -5%: DD1.
    pts = _pts(100_000, 87_000, 95_000)
    cleared_between = _at(1) + timedelta(hours=6)
    assert _breaker_fold(pts, [cleared_between]) is BreakerLevel.DD1


def test_clearance_during_still_deep_drawdown_stays_dd2():
    # cleared, but the next point is still -11%: the computed target IS DD2 —
    # a clearance cannot argue with a live drawdown.
    pts = _pts(100_000, 87_000, 89_000)
    cleared_between = _at(1) + timedelta(hours=6)
    assert _breaker_fold(pts, [cleared_between]) is BreakerLevel.DD2


def test_clearance_after_all_points_applies_at_last_known_state():
    # confirmed after every snapshot: the latch steps down at the LAST point's
    # drawdown (-5% -> DD1) without waiting for the next snapshot — this is
    # the live-point application in _latched_breaker, seen from history alone.
    pts = _pts(100_000, 87_000, 95_000)
    assert _breaker_fold(pts, [_at(2) + timedelta(hours=2)]) is BreakerLevel.DD1


def test_cleared_latch_can_relatch_on_a_new_breach():
    # clear the 87k latch, then a fresh -12% point: DD2 latches again.
    pts = _pts(100_000, 87_000, 95_000, 88_000)
    cleared_between = _at(1) + timedelta(hours=6)
    assert _breaker_fold(pts, [cleared_between]) is BreakerLevel.DD2


def test_clearance_before_any_point_is_inert():
    # nothing was latched when it fired; the later DD2 latch stands.
    pts = _pts(100_000, 87_000, 95_000)
    assert _breaker_fold(pts, [T0 - timedelta(days=1)]) is BreakerLevel.DD2


def test_clearance_with_empty_history_is_calm():
    assert _breaker_fold([], [T0]) is BreakerLevel.NONE
