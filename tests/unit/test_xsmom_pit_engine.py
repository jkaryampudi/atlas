"""Delisting-aware PIT engine + point-in-time eligibility.

Three pillars, all fixture-only:
1. HAND-PINNED delisting liquidation on a tiny panel (the documented rule:
   liquidate at the final available close, pay the per-side cost, proceeds to
   cash) and the unfilled-buy rule (died between decision and execution);
2. EQUIVALENCE: on a delisting-free panel the PIT engine reproduces the frozen
   portfolio.run_portfolio_backtest equity curve exactly (portfolio.py is
   READ-ONLY; the sibling adds only the delisting behaviours);
3. Point-in-time eligibility: a name ranks only while a member — joins late,
   leaves early (end-exclusive), dead name disappears, unseasoned name waits.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.backtest.portfolio import (
    PanelView,
    PricePanel,
    run_portfolio_backtest,
)
from atlas.dcp.backtest.xsmom_pit_run import (
    pit_eligible,
    run_pit_backtest,
    winner_count,
    xsmom_pit_strategy,
)
from atlas.dcp.market_data.index_membership import MembershipRow

COSTS = CostModel()          # 5 + 5 bps per side
SIDE = 0.001


def weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def flat_series(n: int, px: float, first: int = 0,
                last: int | None = None) -> list[float | None]:
    last = n - 1 if last is None else last
    return [px if first <= i <= last else None for i in range(n)]


def member(ticker: str, start: date | None, end: date | None, *,
           active: bool = False, delisted: bool = False) -> MembershipRow:
    return MembershipRow(index_code="GSPC.INDX", ticker=ticker, name=ticker,
                         start_date=start, end_date=end, is_active_now=active,
                         is_delisted=delisted)


# --- 1. hand-pinned delisting rule -------------------------------------------

# Seven sessions spanning a month boundary: rebalance decided at t=2 (Jan 31),
# executed at t=3 (Feb 1). L lives throughout at 100; D trades at 10.
DATES = [date(2024, 1, 29), date(2024, 1, 30), date(2024, 1, 31),
         date(2024, 2, 1), date(2024, 2, 2), date(2024, 2, 5),
         date(2024, 2, 6)]


def half_half(view: PanelView) -> dict[str, float]:
    return {"D": 0.5, "L": 0.5}


def test_forced_liquidation_hand_pinned():
    """D dies after Feb 2 (last bar index 4). Buy-in pays 1.0 turnover * 10bps
    -> equity 0.999. At the first barless session (Feb 5) D is liquidated at
    its final available close (the existing mark, value-neutral) paying the
    per-side cost on its 0.5 weight: 0.999 * (1 - 0.5*0.001) = 0.9985005, and
    the proceeds stay in cash to the end."""
    n = len(DATES)
    panel = PricePanel(
        dates=DATES,
        opens={"D": flat_series(n, 10.0, last=4), "L": flat_series(n, 100.0)},
        closes={"D": flat_series(n, 10.0, last=4), "L": flat_series(n, 100.0)})
    out = run_pit_backtest(panel, half_half, COSTS, start=DATES[0])
    expected = [1.0, 1.0, 1.0, 0.999, 0.999, 0.9985005, 0.9985005]
    assert out.result.equity_curve == pytest.approx(expected, abs=1e-12)
    assert out.result.n_rebalances == 1
    assert out.unfilled_buys == ()
    assert len(out.forced_liquidations) == 1
    fl = out.forced_liquidations[0]
    assert fl.day == date(2024, 2, 5)      # first session WITHOUT a bar
    assert fl.symbol == "D"
    assert fl.weight == pytest.approx(0.5)
    # surviving weight renormalises against post-cost equity; L flat -> curve flat
    assert out.result.total_return == pytest.approx(-0.0014995, abs=1e-12)


def test_unfilled_buy_hand_pinned():
    """D dies between the decision close (Jan 31, has a price) and the
    execution open (Feb 1, no bar): the buy does not fill, that weight stays
    in cash, and only L's 0.5 pays turnover cost -> 1 - 0.5*0.001 = 0.9995."""
    n = len(DATES)
    panel = PricePanel(
        dates=DATES,
        opens={"D": flat_series(n, 10.0, last=2), "L": flat_series(n, 100.0)},
        closes={"D": flat_series(n, 10.0, last=2), "L": flat_series(n, 100.0)})
    out = run_pit_backtest(panel, half_half, COSTS, start=DATES[0])
    expected = [1.0, 1.0, 1.0, 0.9995, 0.9995, 0.9995, 0.9995]
    assert out.result.equity_curve == pytest.approx(expected, abs=1e-12)
    assert out.forced_liquidations == ()
    assert out.unfilled_buys == ((date(2024, 2, 1), "D"),)


def test_frozen_engine_refuses_the_same_panel():
    """Why the sibling exists: the frozen engine fails closed on a mid-hold
    missing price — dead names are impossible there by design."""
    n = len(DATES)
    panel = PricePanel(
        dates=DATES,
        opens={"D": flat_series(n, 10.0, last=4), "L": flat_series(n, 100.0)},
        closes={"D": flat_series(n, 10.0, last=4), "L": flat_series(n, 100.0)})
    with pytest.raises(ValueError, match="missing price while held"):
        run_portfolio_backtest(panel, half_half, COSTS, start=DATES[0])


# --- 2. equivalence on a delisting-free panel ---------------------------------

def test_matches_frozen_engine_without_delistings():
    days = weekdays(date(2024, 1, 2), 130)
    n = len(days)

    def px(seed: int, i: int) -> float:
        return 100.0 + seed * 3.0 + ((i * (seed + 7)) % 17) - 8 + i * 0.05

    opens: dict[str, list[float | None]] = {}
    closes: dict[str, list[float | None]] = {}
    for k, s in enumerate(["AAA", "BBB", "CCC"]):
        closes[s] = [px(k, i) for i in range(n)]
        opens[s] = [px(k, i) - 0.5 for i in range(n)]
    panel = PricePanel(dates=days, opens=opens, closes=closes)

    def top2(view: PanelView) -> dict[str, float]:
        t = view.t
        ranked = sorted(
            view.symbols(),
            key=lambda s: -(view.close(s, t) / view.close(s, max(t - 20, 0))))  # type: ignore[operator]
        return {s: 0.5 for s in ranked[:2]}

    frozen = run_portfolio_backtest(panel, top2, COSTS, start=days[0])
    pit = run_pit_backtest(panel, top2, COSTS, start=days[0])
    assert pit.result.equity_curve == frozen.equity_curve       # exact
    assert pit.result.total_return == frozen.total_return
    assert pit.result.sharpe == pytest.approx(frozen.sharpe)
    assert pit.result.max_drawdown == frozen.max_drawdown
    assert pit.result.avg_turnover == frozen.avg_turnover
    assert pit.result.n_rebalances == frozen.n_rebalances
    assert pit.forced_liquidations == () and pit.unfilled_buys == ()


# --- 3. point-in-time eligibility ---------------------------------------------

def eligibility_panel() -> tuple[PricePanel, dict[str, MembershipRow]]:
    """320 sessions; all series flat except construction-specific spans:
    - CORE   member forever, full series          -> eligible once seasoned
    - LATE   joins at session 280's date          -> eligible only after joining
    - GONE   leaves at session 280's date         -> ineligible from that day (end-exclusive)
    - DEAD   delisted: series ends at session 260 -> disappears with its data
    - YOUNG  member forever, series starts at 10  -> waits for 252 prior sessions
    - NULLA  null start, active now               -> member from the window start
    - NULLD  null start, delisted                 -> excluded fail-closed, never eligible
    """
    days = weekdays(date(2012, 1, 2), 320)
    n = len(days)
    opens = {"CORE": flat_series(n, 50.0), "LATE": flat_series(n, 50.0),
             "GONE": flat_series(n, 50.0), "DEAD": flat_series(n, 50.0, last=260),
             "YOUNG": flat_series(n, 50.0, first=10),
             "NULLA": flat_series(n, 50.0), "NULLD": flat_series(n, 50.0)}
    closes = {s: list(v) for s, v in opens.items()}
    panel = PricePanel(dates=days, opens=opens, closes=closes)
    epoch = date(2000, 1, 3)
    members = {
        "CORE": member("CORE", epoch, None, active=True),
        "LATE": member("LATE", days[280], None, active=True),
        "GONE": member("GONE", epoch, days[280]),
        "DEAD": member("DEAD", epoch, days[262], delisted=True),
        "YOUNG": member("YOUNG", epoch, None, active=True),
        "NULLA": member("NULLA", None, None, active=True),
        "NULLD": member("NULLD", None, days[300], delisted=True),
    }
    return panel, members


def test_pit_eligibility_joins_leaves_dies_seasons():
    panel, members = eligibility_panel()

    at_270 = pit_eligible(PanelView(panel, 270), members)
    # LATE not yet a member; DEAD's series already ended (no price at t);
    # NULLD excluded fail-closed despite having prices and an interval end
    assert at_270 == ["CORE", "GONE", "NULLA", "YOUNG"]

    at_280 = pit_eligible(PanelView(panel, 280), members)
    assert "LATE" in at_280                    # joined exactly today
    assert "GONE" not in at_280                # end-exclusive: gone ON its end date
    at_279 = pit_eligible(PanelView(panel, 279), members)
    assert "GONE" in at_279 and "LATE" not in at_279

    # seasoning: YOUNG's first bar is session 10 -> close(t-252) exists from t=262
    assert "YOUNG" not in pit_eligible(PanelView(panel, 261), members)
    assert "YOUNG" in pit_eligible(PanelView(panel, 262), members)

    # DEAD held prices until 260 and membership until 262: eligible at 260,
    # gone at 261 with its data — a dead name disappears from the ranking
    assert "DEAD" in pit_eligible(PanelView(panel, 260), members)
    assert "DEAD" not in pit_eligible(PanelView(panel, 261), members)

    # NULLA (null start, active) is a member from the window start
    assert "NULLA" in pit_eligible(PanelView(panel, 260), members)
    # NULLD never appears anywhere
    for t in (260, 270, 280, 299):
        assert "NULLD" not in pit_eligible(PanelView(panel, t), members)


def test_strategy_ranks_only_members():
    """xsmom_pit_strategy ranks the eligible set by the same 12-1 recipe and
    never weights a non-member, an unseasoned name or a dead series."""
    panel, members = eligibility_panel()
    strat = xsmom_pit_strategy(members)
    w = strat(PanelView(panel, 270))
    # 4 eligible < TOP_N floor -> hold all, equal weight
    assert set(w) == {"CORE", "GONE", "NULLA", "YOUNG"}
    assert sum(w.values()) == pytest.approx(1.0)
    assert all(x == pytest.approx(0.25) for x in w.values())


def test_winner_count_decile_with_floor():
    assert winner_count(339) == 33
    assert winner_count(500) == 50
    assert winner_count(95) == 10          # floor at v1's TOP_N
    assert winner_count(9) == 10           # ranked[:10] of 9 -> holds all 9
    assert winner_count(100) == 10
