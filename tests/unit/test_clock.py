from datetime import UTC, datetime

import pytest

from atlas.core.clock import FrozenClock, SystemClock


def test_system_clock_is_aware():
    assert SystemClock().now().tzinfo is not None


def test_frozen_clock_deterministic_and_monotonic():
    t0 = datetime(2026, 7, 10, 6, 0, tzinfo=UTC)
    c = FrozenClock(t0)
    assert c.now() == t0
    with pytest.raises(ValueError):
        c.advance_to(datetime(2026, 7, 9, tzinfo=UTC))
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 7, 10))  # naive rejected
