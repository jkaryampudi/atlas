"""xsmom_run validation (symbols) mode — the survivorship cross-check pieces:

- xsmom_top(TOP_N) must reproduce the READ-ONLY signals.xsmom.v1 recipe
  weight-for-weight (the factory exists only because v1 pins TOP_N=10; any
  drift between the two is a silent recipe change);
- the proportional top-3 portfolio is exactly the 3 highest 12-1 returns,
  equal weight, deterministic tie-break;
- annual-outcome helpers (Principal's dispersion request): calendar-year
  returns with honest partial-year flags, seeded moving-block bootstrap with
  PAIRED draws for same-length series, linear-interpolation percentiles —
  golden pins, house style.
"""
from __future__ import annotations

import math
from datetime import date

import pytest

from atlas.dcp.backtest.portfolio import PanelView, PortfolioResult, PricePanel
from atlas.dcp.backtest.xsmom_run import (
    block_bootstrap_annual,
    calendar_year_returns,
    percentile,
    xsmom_top,
)
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.signals.xsmom.v1 import SEASONING, TOP_N, xsmom_v1

SESSIONS = trading_days_between("US", date(2024, 1, 2), date(2025, 3, 31))


def _panel(n_symbols: int) -> PricePanel:
    """Distinct exponential drifts -> a strict, known 12-1 ranking: higher k
    drifts higher, so the top-N by momentum is the top-N by k."""
    dates = SESSIONS[: SEASONING + 5]
    closes = {f"S{k:02d}": [100.0 * math.exp((-0.001 + 0.0003 * k) * i)
                            for i in range(len(dates))]
              for k in range(n_symbols)}
    return PricePanel(dates=dates,
                      opens={s: list(c) for s, c in closes.items()},
                      closes={s: [float(x) for x in c] for s, c in closes.items()})


def test_xsmom_top_at_top_n_reproduces_v1_weight_for_weight():
    panel = _panel(12)
    for t in (SEASONING, SEASONING + 3):
        view = PanelView(panel, t)
        assert xsmom_top(TOP_N)(view) == xsmom_v1(view)


def test_xsmom_top_3_is_the_three_highest_momentum_names_equal_weight():
    panel = _panel(9)
    view = PanelView(panel, SEASONING)
    weights = xsmom_top(3)(view)
    # drifts rise with k, so the winner third is S06, S07, S08
    assert weights == {"S06": pytest.approx(1 / 3), "S07": pytest.approx(1 / 3),
                       "S08": pytest.approx(1 / 3)}


def test_xsmom_top_refuses_nonpositive_n():
    with pytest.raises(ValueError, match="top_n must be >= 1"):
        xsmom_top(0)


def _result(dates: list[date], curve: list[float]) -> PortfolioResult:
    return PortfolioResult(total_return=curve[-1] - 1.0, sharpe=0.0,
                           max_drawdown=0.0, avg_turnover=0.0, n_rebalances=0,
                           equity_curve=curve, dates=dates)


def test_calendar_year_returns_with_partial_year_flags():
    # window 2023-07-03 .. 2025-03-31: 2023 partial (starts mid-year),
    # 2024 full, 2025 partial (ends mid-year)
    dates = trading_days_between("US", date(2023, 7, 3), date(2025, 3, 31))
    curve = [1.0 + 0.001 * i for i in range(len(dates))]
    years = calendar_year_returns(_result(dates, curve))
    last23 = max(i for i, d in enumerate(dates) if d.year == 2023)
    last24 = max(i for i, d in enumerate(dates) if d.year == 2024)
    assert [(y.year, y.partial) for y in years] == [
        (2023, True), (2024, False), (2025, True)]
    assert years[0].ret == pytest.approx(curve[last23] / curve[0] - 1)
    assert years[1].ret == pytest.approx(curve[last24] / curve[last23] - 1)
    assert years[2].ret == pytest.approx(curve[-1] / curve[last24] - 1)
    assert years[0].note == "partial (from 2023-07-03)"
    assert years[2].note == "partial (through 2025-03-31)"
    assert years[1].note == ""


def test_calendar_year_returns_full_first_year_not_flagged():
    dates = trading_days_between("US", date(2024, 1, 1), date(2024, 12, 31))
    years = calendar_year_returns(_result(dates, [1.0] * len(dates)))
    assert [(y.year, y.partial, y.ret) for y in years] == [(2024, False, 0.0)]


def test_block_bootstrap_constant_series_pins_the_compounding():
    draws = block_bootstrap_annual([0.001] * 300, draws=25, seed=7)
    assert len(draws) == 25
    expected = 1.001 ** 252 - 1
    assert all(x == pytest.approx(expected) for x in draws)


def test_block_bootstrap_is_seeded_and_seed_sensitive():
    rets = [0.01 * math.sin(i) for i in range(120)]
    a = block_bootstrap_annual(rets, draws=40, seed=7)
    b = block_bootstrap_annual(rets, draws=40, seed=7)
    c = block_bootstrap_annual(rets, draws=40, seed=8)
    assert a == b
    assert a != c


def test_block_bootstrap_draws_are_paired_for_same_length_series():
    """The report claims strategy and SPY draws are PAIRED (same block
    positions). Indicator series: doubling every daily return must double
    each draw to first order — only true when the block choices coincide."""
    base = [i * 1e-9 for i in range(300)]
    doubled = [2 * x for x in base]
    a = block_bootstrap_annual(base, draws=50, seed=7)
    b = block_bootstrap_annual(doubled, draws=50, seed=7)
    for x, y in zip(a, b):
        assert y == pytest.approx(2 * x, rel=1e-3)


def test_block_bootstrap_refuses_thin_series():
    with pytest.raises(ValueError, match="need >= 21 daily returns"):
        block_bootstrap_annual([0.01] * 20)


def test_percentile_linear_interpolation_golden():
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.5) == pytest.approx(2.5)
    assert percentile([10.0, 20.0, 30.0, 40.0, 50.0], 0.25) == pytest.approx(20.0)
    assert percentile([float(i) for i in range(11)], 0.10) == pytest.approx(1.0)
    assert percentile([7.0], 0.9) == 7.0
    with pytest.raises(ValueError, match="empty sample"):
        percentile([], 0.5)
    with pytest.raises(ValueError, match="q must be in"):
        percentile([1.0], 1.5)
