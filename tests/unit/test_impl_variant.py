"""Implementable-variant machinery (board item 5) — fixture-only unit tests.

Pillars:
1. LIVE CONSTRUCTION pinned: each sleeve holds its top-SLEEVE_N (=5, the
   production SLEEVE_MAX_NAMES, imported not restated) equal weight; fewer
   eligible -> hold them all; none -> that budget is cash; the combined
   satellite is 50/50 with overlapping names summing.
2. The point-in-time large-cap filter (AdvSelector) is deterministic, honours
   the TOP_UNIVERSE cut by trailing mean dollar volume, and reads ONLY data
   <= t — perturbing dollar volumes strictly after t cannot change the base.
3. The monkey null uses the IDENTICAL cached eligible sets and construction:
   when exactly SLEEVE_N names are eligible the monkey IS the strategy
   (weights are forced), pinned path by path; seeded runs are reproducible.
4. Gate/threshold discipline: constants imported from the committed modules,
   never restated here (equalities pinned); pre-committed kill families named
   from the imported KILL_START.
5. truncate_panel reproduces an ended-earlier world exactly: cut, drop
   all-None series, refuse to drop the benchmark.
"""
from __future__ import annotations

import inspect
from datetime import date, timedelta

import pytest

from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.backtest.impl_variant_run import (
    ADV_WINDOW,
    FAMILY_COMBINED,
    FAMILY_PEAD,
    FAMILY_XSMOM,
    SLEEVE_N,
    TOP_UNIVERSE,
    AdvSelector,
    ImplSleeves,
    impl_equal_weight,
    impl_family,
    impl_null_results,
    impl_strategy,
    truncate_panel,
)
from atlas.dcp.backtest.portfolio import PanelView, PricePanel
from atlas.dcp.backtest.validation import null_model_gate
from atlas.dcp.backtest.xsmom_pit_run import KILL_START, run_pit_backtest
from atlas.dcp.market_data.index_membership import MembershipRow
from atlas.dcp.signals.pead.generate import SLEEVE_MAX_NAMES as PEAD_CAP
from atlas.dcp.signals.pead.v1 import EarningsView, SignalEvent
from atlas.dcp.signals.xsmom.generate import SLEEVE_MAX_NAMES
from atlas.dcp.signals.xsmom.v1 import SEASONING

COSTS = CostModel()


# --------------------------------------------------------------- fixtures ---

def weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def member(ticker: str) -> MembershipRow:
    return MembershipRow(index_code="GSPC.INDX", ticker=ticker, name=ticker,
                         start_date=date(2000, 1, 3), end_date=None,
                         is_active_now=True, is_delisted=False)


def growth_series(n: int, start_px: float, rate: float) -> list[float | None]:
    return [start_px * (1.0 + rate) ** i for i in range(n)]


def make_world(n_symbols: int, n_sessions: int = SEASONING + 30,
               ) -> tuple[PricePanel, dict[str, MembershipRow],
                          dict[str, list[float | None]]]:
    """`n_symbols` seasoned members SYM00..; SYMkk grows at k bps/day (so the
    12-1 momentum ranking is the reverse-alphabetical order) and trades
    (k+1) * 1e6 dollars/day flat (so the ADV ranking is also
    reverse-alphabetical: the LAST symbols are the largest)."""
    dates = weekdays(date(2024, 1, 1), n_sessions)
    syms = [f"SYM{k:02d}" for k in range(n_symbols)]
    opens: dict[str, list[float | None]] = {}
    closes: dict[str, list[float | None]] = {}
    dv: dict[str, list[float | None]] = {}
    for k, s in enumerate(syms):
        series = growth_series(n_sessions, 100.0, 0.0001 * k)
        opens[s] = list(series)
        closes[s] = list(series)
        dv[s] = [(k + 1) * 1e6] * n_sessions
    panel = PricePanel(dates=dates, opens=opens, closes=closes)
    return panel, {s: member(s) for s in syms}, dv


def live_sue_view(t: int, sues: dict[str, float]) -> EarningsView:
    """An EarningsView with one fresh (effective at t-1), defined SUE per
    symbol — constructed directly so the ranking inputs are hand-pinned."""
    return EarningsView({
        s: [SignalEvent(effective_index=t - 1, report_date=date(2024, 12, 1),
                        sue=v, surprise_pct=v)]
        for s, v in sues.items()})


# ------------------------------------------------- 1. live construction ---

def test_sleeve_cap_is_the_production_cap_imported():
    assert SLEEVE_N == SLEEVE_MAX_NAMES == PEAD_CAP == 5


def test_momentum_top5_equal_weight():
    """Ranking is reverse-alphabetical by construction; the sleeve holds the
    top 5 at exactly 0.2 each — the live book shape, not the decile."""
    panel, members, dv = make_world(12)
    sleeves = ImplSleeves(AdvSelector(members, dv), EarningsView({}))
    t = len(panel.dates) - 1
    w = impl_strategy(sleeves, "xsmom")(PanelView(panel, t))
    assert w == pytest.approx({f"SYM{k:02d}": 0.2 for k in range(7, 12)})


def test_fewer_than_five_hold_all_never_pad():
    panel, members, dv = make_world(3)
    sleeves = ImplSleeves(AdvSelector(members, dv), EarningsView({}))
    t = len(panel.dates) - 1
    w = impl_strategy(sleeves, "xsmom")(PanelView(panel, t))
    assert w == pytest.approx({s: 1.0 / 3.0 for s in ("SYM00", "SYM01", "SYM02")})


def test_pead_top5_ranks_by_live_sue():
    panel, members, dv = make_world(8)
    t = len(panel.dates) - 1
    sues = {f"SYM{k:02d}": float(k) for k in range(8)}  # SYM07 best
    sleeves = ImplSleeves(AdvSelector(members, dv), live_sue_view(t, sues))
    w = impl_strategy(sleeves, "pead")(PanelView(panel, t))
    assert w == pytest.approx({f"SYM{k:02d}": 0.2 for k in range(3, 8)})


def test_pead_without_signals_is_all_cash():
    panel, members, dv = make_world(8)
    sleeves = ImplSleeves(AdvSelector(members, dv), EarningsView({}))
    assert impl_strategy(sleeves, "pead")(
        PanelView(panel, len(panel.dates) - 1)) == {}


def test_combined_is_50_50_with_overlap_summing():
    """Momentum picks SYM07..SYM11; SUE is alphabetical-ascending on
    SYM00..SYM07 so PEAD picks SYM03..SYM07. SYM07 sits in both sleeves and
    carries 0.1 + 0.1; every other pick carries 0.1; gross is exactly 1.0."""
    panel, members, dv = make_world(12)
    t = len(panel.dates) - 1
    sues = {f"SYM{k:02d}": float(k) for k in range(8)}
    sleeves = ImplSleeves(AdvSelector(members, dv), live_sue_view(t, sues))
    w = impl_strategy(sleeves, "combined")(PanelView(panel, t))
    expected = {f"SYM{k:02d}": 0.1 for k in range(3, 12)}
    expected["SYM07"] = 0.2
    assert w == pytest.approx(expected)
    assert sum(w.values()) == pytest.approx(1.0)


def test_combined_empty_pead_sleeve_holds_cash():
    """No live signals: the PEAD half is cash (gross 0.5) — the live
    behaviour (no signal, no position), never reallocated to momentum."""
    panel, members, dv = make_world(12)
    sleeves = ImplSleeves(AdvSelector(members, dv), EarningsView({}))
    w = impl_strategy(sleeves, "combined")(PanelView(panel, len(panel.dates) - 1))
    assert w == pytest.approx({f"SYM{k:02d}": 0.1 for k in range(7, 12)})
    assert sum(w.values()) == pytest.approx(0.5)


def test_equal_weight_benchmark_spans_the_whole_base():
    panel, members, dv = make_world(12)
    t = len(panel.dates) - 1
    sues = {f"SYM{k:02d}": float(k) for k in range(8)}
    sleeves = ImplSleeves(AdvSelector(members, dv), live_sue_view(t, sues))
    w = impl_equal_weight(sleeves, "combined")(PanelView(panel, t))
    # momentum half: 0.5/12 over all 12; PEAD half: 0.5/8 over the 8 with SUE
    for k in range(12):
        expected = 0.5 / 12 + (0.5 / 8 if k < 8 else 0.0)
        assert w[f"SYM{k:02d}"] == pytest.approx(expected)
    assert sum(w.values()) == pytest.approx(1.0)


# ------------------------------- 2. the point-in-time large-cap filter ---

def test_adv_filter_cuts_at_top_universe():
    """103 eligible names; the three smallest by trailing dollar volume
    (SYM00..SYM02) fall outside the top-100 base."""
    panel, members, dv = make_world(103)
    sleeves = ImplSleeves(AdvSelector(members, dv), EarningsView({}))
    base = sleeves.momentum_base(PanelView(panel, len(panel.dates) - 1))
    assert len(base) == TOP_UNIVERSE
    assert {"SYM00", "SYM01", "SYM02"}.isdisjoint(base)
    assert "SYM03" in base and "SYM102" in base


def test_adv_filter_reads_only_data_up_to_t():
    """Perturbing dollar volumes STRICTLY AFTER t (making the excluded name
    the biggest trader of the future) cannot change the base at t; the same
    perturbation INSIDE the trailing window does. No look-ahead, structurally."""
    panel, members, dv = make_world(103)
    t = len(panel.dates) - 10
    base0 = AdvSelector(members, dv).base(PanelView(panel, t))
    future = {s: list(v) for s, v in dv.items()}
    for i in range(t + 1, len(panel.dates)):
        future["SYM00"][i] = 1e12
    assert AdvSelector(members, future).base(PanelView(panel, t)) == base0
    inside = {s: list(v) for s, v in dv.items()}
    inside["SYM00"][t] = 1e12
    assert "SYM00" in AdvSelector(members, inside).base(PanelView(panel, t))


def test_adv_is_trailing_mean_hand_pinned():
    """SYM00 prints 300e6 dollars on the FINAL session and zero elsewhere.
    Trailing mean at t: 300e6/63 ≈ 4.76e6 — above SYM03's flat 4e6 (the
    100th-largest name, i.e. the cut line with three excluded of 103) and
    below SYM04's 5e6, so the single print lifts SYM00 into the base at t and
    pushes SYM03 out. One session earlier the window ends BEFORE the print:
    mean 0, out. Pins both the mean arithmetic and the window edge."""
    panel, members, dv = make_world(103)
    t = len(panel.dates) - 1
    burst = {s: list(v) for s, v in dv.items()}
    burst["SYM00"] = [0.0] * len(panel.dates)
    burst["SYM00"][t] = 300e6
    sel = AdvSelector(members, burst)
    assert 4e6 < 300e6 / ADV_WINDOW < 5e6      # the pin is unambiguous
    at_t = sel.base(PanelView(panel, t))
    assert "SYM00" in at_t and "SYM03" not in at_t
    assert "SYM00" not in sel.base(PanelView(panel, t - 1))
    # cached: same tuple object on re-query (pure property of t)
    assert sel.base(PanelView(panel, t)) is sel.base(PanelView(panel, t))


def test_unseasoned_and_nonmember_names_never_enter_the_base():
    """SYM11 holds no membership row; SYM10 lists too late to be seasoned at
    t (its close at t - SEASONING is missing). Neither can enter the base —
    the filter layers on pit_eligible, imported unchanged."""
    panel, members, dv = make_world(12)
    members = dict(members)
    members.pop("SYM11")
    t = len(panel.dates) - 1
    opens = {s: list(v) for s, v in panel.opens.items()}
    closes = {s: list(v) for s, v in panel.closes.items()}
    for i in range(t - SEASONING + 1):           # listed after t - SEASONING
        opens["SYM10"][i] = None
        closes["SYM10"][i] = None
    clean = PricePanel(dates=panel.dates, opens=opens, closes=closes)
    base = AdvSelector(members, dv).base(PanelView(clean, t))
    assert "SYM11" not in base       # not a member
    assert "SYM10" not in base       # < SEASONING sessions of history
    assert "SYM09" in base


# ----------------------------------------- 3. the fair monkey null ---------

def test_monkey_with_exactly_five_eligible_is_the_strategy():
    """|base| == SLEEVE_N forces the monkey's hand: rng.sample must return all
    five names, so every null path equals the strategy path exactly — the
    strongest possible 'same set, same construction' pin."""
    panel, members, dv = make_world(5)
    sleeves = ImplSleeves(AdvSelector(members, dv), EarningsView({}))
    start = panel.dates[0]
    strat = run_pit_backtest(panel, impl_strategy(sleeves, "xsmom"), COSTS,
                             start=start).result
    nulls = impl_null_results(panel, sleeves, "xsmom", costs=COSTS,
                              start=start, paths=3, seed=11)
    for r in nulls:
        assert r.equity_curve == pytest.approx(strat.equity_curve)


def test_monkey_null_is_seed_deterministic():
    panel, members, dv = make_world(12)
    sleeves = ImplSleeves(AdvSelector(members, dv), EarningsView({}))
    kw = dict(costs=COSTS, start=panel.dates[0], paths=4, seed=7)
    a = impl_null_results(panel, sleeves, "xsmom", **kw)
    b = impl_null_results(panel, sleeves, "xsmom", **kw)
    assert [r.total_return for r in a] == [r.total_return for r in b]
    assert len({round(r.total_return, 12) for r in a}) > 1  # paths do differ


# --------------------------- 4. thresholds, families, pre-commitments ------

def test_thresholds_are_imported_not_restated():
    """The gate thresholds this runner judges against ARE the committed
    signature defaults of validation.null_model_gate — one source of truth."""
    from atlas.dcp.backtest.impl_variant_run import DSR_MIN, P_MAX
    params = inspect.signature(null_model_gate).parameters
    assert P_MAX == params["p_max"].default
    assert DSR_MIN == params["dsr_min"].default


def test_families_and_kill_naming():
    assert (FAMILY_XSMOM, FAMILY_PEAD, FAMILY_COMBINED) == (
        "xsmom-impl-tr", "pead-impl-tr", "combined-impl-tr")
    assert KILL_START == date(2016, 1, 1)   # the board's pre-commitment
    assert impl_family("xsmom", None) == "xsmom-impl-tr"
    assert impl_family("combined", KILL_START) == "combined-impl-tr-2016"
    with pytest.raises(KeyError):
        impl_family("sharpe-maximizer-9000", None)


def test_universe_constants_pinned():
    """TOP_UNIVERSE is the S&P 100 size the ADR-0007 book approximates;
    ADV_WINDOW is the one-quarter convention (= PEAD's staleness window)."""
    from atlas.dcp.signals.pead.v1 import STALENESS_SESSIONS
    assert TOP_UNIVERSE == 100
    assert ADV_WINDOW == 63 == STALENESS_SESSIONS


# ------------------------------------------------- 5. panel truncation -----

def test_truncate_panel_cuts_and_drops_late_series():
    n = 40
    dates = weekdays(date(2024, 1, 1), n)
    flat: list[float | None] = [100.0] * n
    late: list[float | None] = [None] * 30 + [50.0] * 10
    panel = PricePanel(dates=dates, opens={"SPY": list(flat), "LATE": list(late)},
                       closes={"SPY": list(flat), "LATE": list(late)})
    cut, dropped = truncate_panel(panel, dates[19])
    assert cut.dates == dates[:20]
    assert dropped == ("LATE",)
    assert set(cut.closes) == {"SPY"}
    # cutting past the end is a no-op that keeps the original panel
    same, none_dropped = truncate_panel(panel, dates[-1] + timedelta(days=30))
    assert same is panel and none_dropped == ()


def test_truncate_panel_refuses_to_drop_the_benchmark():
    n = 40
    dates = weekdays(date(2024, 1, 1), n)
    late: list[float | None] = [None] * 30 + [400.0] * 10
    flat: list[float | None] = [100.0] * n
    panel = PricePanel(dates=dates, opens={"SPY": list(late), "X": list(flat)},
                       closes={"SPY": list(late), "X": list(flat)})
    with pytest.raises(RuntimeError, match="benchmark"):
        truncate_panel(panel, dates[19])
