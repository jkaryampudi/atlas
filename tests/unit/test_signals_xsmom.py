"""xsmom v1 (Jegadeesh-Titman 12-1, top 10 equal weight): hand-pinned rank
selection including the 21-session skip, the 252-session seasoning boundary
(point-in-time listing honesty), fewer-than-10 edge cases, deterministic
tie-breaks, and the structural no-look-ahead property through the portfolio
engine — house style, mirrors test_signals_trend.py."""
import math
import random
from datetime import date, timedelta

import pytest

from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.backtest.portfolio import PanelView, PricePanel, run_portfolio_backtest
from atlas.dcp.signals.xsmom.v1 import (
    LOOKBACK,
    SEASONING,
    SKIP,
    SPEC,
    TOP_N,
    eligible_symbols,
    xsmom_v1,
)

COSTS = CostModel()
N = 300


def weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DATES = weekdays(date(2023, 1, 2), N)


def step_series(level_after: float, step_at: int, n: int = N,
                base: float = 100.0) -> list[float | None]:
    """base until step_at (exclusive), then base*level_after."""
    return [base if i < step_at else base * level_after for i in range(n)]


def make_panel(closes: dict[str, list[float | None]]) -> PricePanel:
    return PricePanel(dates=DATES, closes=closes,
                      opens={s: list(v) for s, v in closes.items()})


def test_spec_is_textbook_and_family_named():
    assert SPEC["family"] == "xsmom"
    assert (SPEC["lookback_sessions"], SPEC["skip_sessions"], SPEC["top_n"],
            SPEC["seasoning_sessions"]) == (252, 21, 10, 252)
    assert SPEC["name"] == "jt_12_1_top10"
    assert "no search" in str(SPEC["provenance"])
    assert (LOOKBACK, SKIP, TOP_N, SEASONING) == (252, 21, 10, 252)


def test_rank_pins_top10_and_21_session_skip():
    """At t=280 the formation window is [28, 259]. S00..S11 step at 130 to
    returns 1.2, 1.1, ..., 0.1; SPIK is flat through 259 (formation return
    0.0) then quintuples inside the skip window. The winner decile is
    S00..S09 at 0.1 each; SPIK's recent surge must NOT rank (the skip is the
    whole point of 12-1) and S10/S11 fall below the cut."""
    t = 280
    closes: dict[str, list[float | None]] = {
        f"S{k:02d}": step_series(1.0 + (12 - k) / 10, 130) for k in range(12)}
    closes["SPIK"] = [100.0 if i <= t - SKIP else 500.0 for i in range(N)]
    view = PanelView(make_panel(closes), t)
    assert eligible_symbols(view) == sorted(closes)      # all seasoned at 280
    targets = xsmom_v1(view)
    assert set(targets) == {f"S{k:02d}" for k in range(10)}
    assert all(w == pytest.approx(0.1) for w in targets.values())
    assert "SPIK" not in targets
    # sanity of the pin itself: SPIK's unskipped return would have ranked #1
    spik_unskipped = 500.0 / 100.0 - 1.0
    assert spik_unskipped > 1.2


def test_seasoning_boundary_is_point_in_time():
    """YOUNG lists at index 29: at t=280 it has 251 prior sessions (close at
    t-252=28 is None) -> ineligible; one session later it is seasoned and its
    3.0 formation return immediately ranks it. Listings join when seasoned,
    never retroactively."""
    closes: dict[str, list[float | None]] = {
        f"S{k:02d}": step_series(1.0 + (12 - k) / 10, 130) for k in range(12)}
    closes["YOUNG"] = [None] * 29 + [100.0 if i < 200 else 400.0
                                     for i in range(29, N)]
    panel = make_panel(closes)
    at_280 = PanelView(panel, 280)
    assert "YOUNG" not in eligible_symbols(at_280)
    assert "YOUNG" not in xsmom_v1(at_280)
    at_281 = PanelView(panel, 281)
    assert "YOUNG" in eligible_symbols(at_281)
    assert "YOUNG" in xsmom_v1(at_281)


def test_fewer_than_top_n_never_pads():
    closes: dict[str, list[float | None]] = {
        "AAA": step_series(1.5, 130), "BBB": step_series(1.2, 130),
        "CCC": step_series(0.8, 130)}
    targets = xsmom_v1(PanelView(make_panel(closes), 280))
    assert set(targets) == {"AAA", "BBB", "CCC"}
    assert all(w == pytest.approx(1 / 3) for w in targets.values())
    assert sum(targets.values()) == pytest.approx(1.0)


def test_no_eligible_names_returns_empty():
    closes: dict[str, list[float | None]] = {"AAA": step_series(1.5, 130)}
    assert xsmom_v1(PanelView(make_panel(closes), 100)) == {}


def test_tie_break_is_alphabetical_and_deterministic():
    """Nine distinct winners fill 9 slots; TIEA and TIEB have identical
    formation returns and compete for the last one: alphabetical wins."""
    closes: dict[str, list[float | None]] = {
        f"W{k}": step_series(2.0 + k / 10, 130) for k in range(9)}
    closes["TIEB"] = step_series(1.4, 130)
    closes["TIEA"] = step_series(1.4, 130)
    view = PanelView(make_panel(closes), 280)
    targets = xsmom_v1(view)
    assert len(targets) == TOP_N
    assert "TIEA" in targets and "TIEB" not in targets
    assert xsmom_v1(view) == targets           # pure function of the view


def _rw_panel(n: int = 330, syms: int = 15, seed: int = 7) -> PricePanel:
    """15 seeded random walks: more names than TOP_N, so the winner decile
    actually churns across rebalances (turnover > 0, rankings vary)."""
    rng = random.Random(seed)
    dates = weekdays(date(2023, 1, 2), n)
    opens: dict[str, list[float | None]] = {}
    closes: dict[str, list[float | None]] = {}
    for k in range(syms):
        px = 100.0
        o: list[float | None] = []
        c: list[float | None] = []
        for _ in range(n):
            o.append(px)
            px *= math.exp(0.012 * rng.gauss(0, 1))
            c.append(px)
        opens[f"R{k}"], closes[f"R{k}"] = o, c
    return PricePanel(dates=dates, opens=opens, closes=closes)


def test_no_look_ahead_through_the_engine():
    """Perturbing FUTURE prices never changes holdings chosen at t <= cut."""
    panel = _rw_panel()
    cut = 300
    corrupted = PricePanel(
        dates=panel.dates,
        opens={s: v[:cut] + [1.0] * (len(v) - cut) for s, v in panel.opens.items()},
        closes={s: v[:cut] + [1.0] * (len(v) - cut) for s, v in panel.closes.items()})
    a: list[tuple[int, tuple[tuple[str, float], ...]]] = []
    b: list[tuple[int, tuple[tuple[str, float], ...]]] = []

    def spy(record):
        def s(view: PanelView) -> dict[str, float]:
            out = xsmom_v1(view)
            record.append((view.t, tuple(sorted(out.items()))))
            return out
        return s

    start = panel.dates[SEASONING]
    run_portfolio_backtest(panel, spy(a), COSTS, start=start)
    run_portfolio_backtest(corrupted, spy(b), COSTS, start=start)
    upto = [d for d in a if d[0] <= cut]
    assert upto and upto == [d for d in b if d[0] <= cut]


def test_costs_strictly_reduce_returns():
    panel = _rw_panel()
    start = panel.dates[SEASONING]
    free = run_portfolio_backtest(panel, xsmom_v1, CostModel(0, 0), start=start)
    paid = run_portfolio_backtest(panel, xsmom_v1, COSTS, start=start)
    assert paid.n_rebalances > 0
    assert free.total_return > paid.total_return
