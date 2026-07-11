from datetime import date
from decimal import Decimal
from pathlib import Path

from atlas.dcp.market_data.adapters.fixture import FixtureAdapter
from atlas.dcp.market_data.adjustment import adjust_for_splits

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_split_adjustment_makes_series_continuous():
    a = FixtureAdapter(FIXTURES)
    bars = a.fetch_bars("AVGO", date(2024, 7, 1), date(2024, 7, 31))
    splits = a.fetch_splits("AVGO", date(2024, 7, 1), date(2024, 7, 31))
    adj = adjust_for_splits(bars, splits)
    pre = next(b for b in adj if b.bar_date == date(2024, 7, 11))
    post = next(b for b in adj if b.bar_date == date(2024, 7, 15))
    assert pre.close == Decimal("172.5")           # 1725 / 10
    assert pre.volume == 21_000_000                # 2.1M * 10
    assert "split_adjusted" in pre.quality_flags
    assert post.close == Decimal("172.50")         # on/after ex-date untouched
    assert "split_adjusted" not in post.quality_flags
    jump = abs(post.close - pre.close) / pre.close
    assert jump < Decimal("0.05")                  # continuity: no artificial 10x gap


def test_no_splits_is_identity():
    a = FixtureAdapter(FIXTURES)
    bars = a.fetch_bars("AVGO", date(2024, 7, 1), date(2024, 7, 12))
    assert adjust_for_splits(bars, []) == bars
