"""Portfolio engine: hand-pinned equity/turnover/cost arithmetic on a tiny
3-symbol 2-rebalance fixture (every number derived by hand in comments), the
structural no-look-ahead property (perturbing FUTURE prices never changes
holdings at t), costs strictly reduce return, and fail-closed validation of
panels and strategy outputs — house style, mirrors test_backtest_engine.py."""
from datetime import date, timedelta

import pytest

from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.backtest.portfolio import (
    PanelView,
    PricePanel,
    month_end_indices,
    run_portfolio_backtest,
    turnover,
)

COSTS = CostModel()          # 5 + 5 bps per side -> side rate 0.001


def weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def weekdays_between(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def flat_panel_series(dates: list[date], px: float) -> list[float | None]:
    return [px] * len(dates)


def tiny_panel() -> PricePanel:
    """Jan-Mar 2024 weekdays. AAA flat 100; CCC flat 200; BBB 50 through Jan,
    gaps up intraday on Feb 1 (open 50 -> close 55), then flat 55."""
    dates = weekdays_between(date(2024, 1, 1), date(2024, 3, 29))
    feb1 = dates.index(date(2024, 2, 1))
    bbb_open: list[float | None] = [50.0] * feb1 + [50.0] + [55.0] * (len(dates) - feb1 - 1)
    bbb_close: list[float | None] = [50.0] * feb1 + [55.0] * (len(dates) - feb1)
    return PricePanel(
        dates=dates,
        opens={"AAA": flat_panel_series(dates, 100.0), "BBB": bbb_open,
               "CCC": flat_panel_series(dates, 200.0)},
        closes={"AAA": flat_panel_series(dates, 100.0), "BBB": bbb_close,
                "CCC": flat_panel_series(dates, 200.0)})


def scripted(view: PanelView) -> dict[str, float]:
    """The fixture's two rebalances — any other call date is a schedule bug."""
    if view.today == date(2024, 1, 31):
        return {"AAA": 0.5, "BBB": 0.5}
    if view.today == date(2024, 2, 29):
        return {"BBB": 1.0}
    raise AssertionError(f"unexpected rebalance call at {view.today}")


def test_hand_pinned_two_rebalance_arithmetic():
    """Every number verified by hand:

    Jan (all cash): equity 1.0. Decision at Jan 31 close: {AAA .5, BBB .5}.
    Feb 1 (execution at open): no prior holdings, turnover = .5 + .5 = 1.0,
      cost = 1.0 x 0.001 -> equity 0.999. Open->close: AAA 100->100 (g=1),
      BBB 50->55 (g=1.1): G = .5*1 + .5*1.1 = 1.05 -> equity 0.999*1.05
      = 1.04895. Drifted weights: AAA .5/1.05 = 10/21, BBB .55/1.05 = 11/21.
    Feb 2..29 flat: equity stays 1.04895. Decision at Feb 29: {BBB 1.0}.
    Mar 1 (execution): drift to open is flat; turnover = |0 - 10/21| +
      |1 - 11/21| = 20/21; equity = 1.04895 x (1 - (20/21)*0.001)
      = 1.04895 - 1.04895/1050 = 1.04895 - 0.000999 = 1.047951.
    Mar flat thereafter. Mar 29 is the window's last session: its month-end
    never trades (no next open), so exactly 2 rebalances.
    """
    panel = tiny_panel()
    r = run_portfolio_backtest(panel, scripted, COSTS, start=date(2024, 1, 1))
    assert r.n_rebalances == 2
    assert r.avg_turnover == pytest.approx((1.0 + 20 / 21) / 2)   # = 41/42
    assert r.total_return == pytest.approx(0.999 * 1.05 * (1 - (20 / 21) * 0.001) - 1)
    feb1_i = panel.dates.index(date(2024, 2, 1))
    assert r.equity_curve[feb1_i] == pytest.approx(1.04895)
    # only dip: the second rebalance's cost, 1/1050 of equity
    assert r.max_drawdown == pytest.approx(-1 / 1050)
    assert len(r.equity_curve) == len(panel.dates) == len(r.dates)


def test_costs_strictly_reduce_returns():
    panel = tiny_panel()
    free = run_portfolio_backtest(panel, scripted, CostModel(0, 0),
                                  start=date(2024, 1, 1))
    paid = run_portfolio_backtest(panel, scripted, COSTS, start=date(2024, 1, 1))
    assert free.total_return == pytest.approx(0.05)   # 1.0 x 1.05 - 1, cost-free
    assert free.total_return > paid.total_return


def test_month_end_indices_last_session_never_rebalances():
    dates = weekdays_between(date(2024, 1, 1), date(2024, 3, 29))
    idxs = month_end_indices(dates, 0, len(dates))
    assert [dates[i] for i in idxs] == [date(2024, 1, 31), date(2024, 2, 29)]
    # a window ending mid-month exposes only completed month ends
    feb15 = dates.index(date(2024, 2, 15)) + 1
    assert [dates[i] for i in month_end_indices(dates, 0, feb15)] == \
        [date(2024, 1, 31)]


def _rw_panel(n: int = 130, syms: int = 4, seed: int = 11) -> PricePanel:
    import math
    import random
    rng = random.Random(seed)
    dates = weekdays(date(2024, 1, 1), n)
    opens: dict[str, list[float | None]] = {}
    closes: dict[str, list[float | None]] = {}
    for k in range(syms):
        px = 100.0
        o: list[float | None] = []
        c: list[float | None] = []
        for _ in range(n):
            o.append(px)
            px *= math.exp(0.01 * rng.gauss(0, 1))
            c.append(px)
        opens[f"S{k}"], closes[f"S{k}"] = o, c
    return PricePanel(dates=dates, opens=opens, closes=closes)


def _top1_by_5d(view: PanelView) -> dict[str, float]:
    scored = []
    for s in view.symbols():
        now, past = view.close(s, view.t), view.close(s, view.t - 5)
        if now is not None and past is not None:
            scored.append((now / past, s))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return {scored[0][1]: 1.0} if scored else {}


def test_no_look_ahead_is_structural():
    """Mutating the FUTURE must not change any past decision (house property
    test, mirrors test_backtest_engine.py)."""
    panel = _rw_panel()
    cut = 80
    corrupted = PricePanel(
        dates=panel.dates,
        opens={s: v[:cut] + [1.0] * (len(v) - cut) for s, v in panel.opens.items()},
        closes={s: v[:cut] + [1.0] * (len(v) - cut) for s, v in panel.closes.items()})
    decisions_a: list[tuple[int, tuple[tuple[str, float], ...]]] = []
    decisions_b: list[tuple[int, tuple[tuple[str, float], ...]]] = []

    def spy(record):
        def s(view: PanelView) -> dict[str, float]:
            out = _top1_by_5d(view)
            record.append((view.t, tuple(sorted(out.items()))))
            return out
        return s

    a = run_portfolio_backtest(panel, spy(decisions_a), COSTS, start=panel.dates[10])
    b = run_portfolio_backtest(corrupted, spy(decisions_b), COSTS, start=panel.dates[10])
    upto = [d for d in decisions_a if d[0] <= cut]
    assert upto and upto == [d for d in decisions_b if d[0] <= cut]
    # equity marked at closes depends only on the past too
    n_common = cut - 10
    assert a.equity_curve[:n_common] == pytest.approx(b.equity_curve[:n_common])


def test_view_lookahead_raises_and_prehistory_is_none():
    panel = tiny_panel()
    view = PanelView(panel, 5)
    with pytest.raises(ValueError, match="look-ahead"):
        view.close("AAA", 6)
    assert view.close("AAA", -1) is None
    assert view.close("AAA", 5) == 100.0
    assert view.n == 6


def test_strategy_output_validation_fails_closed():
    panel = tiny_panel()
    start = date(2024, 1, 1)
    with pytest.raises(ValueError, match="long-only"):
        run_portfolio_backtest(panel, lambda v: {"AAA": -0.1}, COSTS, start=start)
    with pytest.raises(ValueError, match="no leverage"):
        run_portfolio_backtest(panel, lambda v: {"AAA": 0.7, "BBB": 0.7},
                               COSTS, start=start)
    with pytest.raises(ValueError, match="unknown symbol"):
        run_portfolio_backtest(panel, lambda v: {"ZZZ": 0.5}, COSTS, start=start)


def test_targeting_an_unlisted_symbol_fails_closed():
    dates = weekdays_between(date(2024, 1, 1), date(2024, 3, 29))
    late_i = dates.index(date(2024, 2, 15))
    late: list[float | None] = [None] * late_i + [10.0] * (len(dates) - late_i)
    panel = PricePanel(
        dates=dates,
        opens={"AAA": flat_panel_series(dates, 100.0), "LATE": list(late)},
        closes={"AAA": flat_panel_series(dates, 100.0), "LATE": list(late)})
    with pytest.raises(ValueError, match="without a price at decision"):
        run_portfolio_backtest(panel, lambda v: {"LATE": 0.5}, COSTS,
                               start=date(2024, 1, 1))


def test_holding_a_series_that_ends_fails_closed():
    """A held symbol losing its price mid-window must raise, never silently
    mark stale (capital preservation: no phantom marks)."""
    dates = weekdays_between(date(2024, 1, 1), date(2024, 3, 29))
    end_i = dates.index(date(2024, 2, 15))
    dead: list[float | None] = [10.0] * end_i + [None] * (len(dates) - end_i)
    panel = PricePanel(
        dates=dates,
        opens={"AAA": flat_panel_series(dates, 100.0), "DEAD": list(dead)},
        closes={"AAA": flat_panel_series(dates, 100.0), "DEAD": list(dead)})
    with pytest.raises(ValueError, match="missing price while held"):
        run_portfolio_backtest(panel, lambda v: {"DEAD": 1.0}, COSTS,
                               start=date(2024, 1, 1))


def test_panel_validation():
    dates = weekdays(date(2024, 1, 1), 5)
    ok: list[float | None] = [1.0] * 5
    holey: list[float | None] = [1.0, None, 1.0, 1.0, 1.0]
    with pytest.raises(ValueError, match="holes"):
        PricePanel(dates=dates, opens={"A": list(holey)}, closes={"A": list(holey)})
    with pytest.raises(ValueError, match="availability disagree"):
        PricePanel(dates=dates, opens={"A": [None] + ok[1:]}, closes={"A": list(ok)})
    with pytest.raises(ValueError, match="same symbols"):
        PricePanel(dates=dates, opens={"A": list(ok)},
                   closes={"A": list(ok), "B": list(ok)})
    with pytest.raises(ValueError, match="length"):
        PricePanel(dates=dates, opens={"A": ok[:4]}, closes={"A": ok[:4]})
    with pytest.raises(ValueError, match="ascending"):
        PricePanel(dates=list(reversed(dates)), opens={"A": list(ok)},
                   closes={"A": list(ok)})
    with pytest.raises(ValueError, match="no data"):
        PricePanel(dates=dates, opens={"A": [None] * 5}, closes={"A": [None] * 5})


def test_window_and_schedule_guards():
    panel = tiny_panel()
    with pytest.raises(ValueError, match="unsupported rebalance"):
        run_portfolio_backtest(panel, scripted, COSTS,
                               start=date(2024, 1, 1), rebalance="weekly")
    with pytest.raises(ValueError, match="after the panel ends"):
        run_portfolio_backtest(panel, scripted, COSTS, start=date(2025, 1, 1))
    with pytest.raises(ValueError, match="too short"):
        run_portfolio_backtest(panel, scripted, COSTS,
                               start=panel.dates[-1], end=panel.dates[-1])


def test_turnover_counts_both_sides():
    assert turnover({"A": 0.5, "B": 0.5}, {"B": 0.5, "C": 0.5}) == \
        pytest.approx(1.0)                      # sell A (.5) + buy C (.5)
    assert turnover({}, {"A": 1.0}) == pytest.approx(1.0)
    assert turnover({"A": 1.0}, {"A": 1.0}) == 0.0
