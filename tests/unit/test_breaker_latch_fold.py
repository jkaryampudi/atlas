"""Latched breaker fold (Doc 04 §5, review finding): DD2/DD3 must survive NAV
recovery — only the dual-confirmed human action may clear them, and that
action does not exist yet, so they hold. The stateless computed_breaker view
of the same history would clear on recovery; the fold must not."""
from decimal import Decimal

from atlas.dcp.risk.engine import BreakerLevel, computed_breaker, drawdown
from atlas.dcp.trading.proposals import _breaker_fold


def test_dd2_latches_through_recovery():
    navs = [Decimal(100_000), Decimal(87_000), Decimal(95_000)]  # -13% then -5%
    assert _breaker_fold(navs) is BreakerLevel.DD2
    # the stateless view of the final point alone says DD1 — the latch must win
    assert computed_breaker(drawdown(Decimal(95_000), Decimal(100_000))) \
        is BreakerLevel.DD1


def test_dd1_tracks_drawdown_without_latching():
    navs = [Decimal(100_000), Decimal(91_000), Decimal(99_000)]  # -9% then -1%
    assert _breaker_fold(navs) is BreakerLevel.NONE


def test_latched_dd2_still_escalates_to_dd3():
    navs = [Decimal(100_000), Decimal(87_000), Decimal(95_000), Decimal(84_000)]
    assert _breaker_fold(navs) is BreakerLevel.DD3  # -16% overrides the DD2 latch


def test_empty_history_is_calm():
    assert _breaker_fold([Decimal(100_000)]) is BreakerLevel.NONE
