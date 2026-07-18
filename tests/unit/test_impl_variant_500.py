"""Full-universe mode (--top-universe 0; ADR-0016 evidence run) — fixture-only
unit tests. Fixture builders are imported from test_impl_variant so the two
files describe the SAME world.

Pillars:
1. MODE SEMANTICS: top_universe=0 makes the per-rebalance base the ENTIRE
   point-in-time eligible set (sorted) — no ADV cut; structurally no liquidity
   data is read (an empty dollar-volume matrix is valid input in that mode).
2. DEFAULT UNCHANGED: the signature default IS the module TOP_UNIVERSE, and an
   explicit top_universe=TOP_UNIVERSE selects the identical base as the
   two-argument form — byte-identical behaviour without the flag.
3. THE MODE CHANGES THE BOOK where the ADV screen used to bind: a world whose
   momentum winners are its ADV losers holds different top-5 names per mode,
   hand-pinned both ways.
4. FAMILY DISCIPLINE: full-universe families are `xsmom-impl500-tr` (+ the
   pre-committed kill sibling); pead/combined are REFUSED (ADR-0015: PEAD
   sleeve budget 0) — no unsanctioned family can be registered.
5. The monkey null keeps drawing from the IDENTICAL (now full) eligible set:
   forced-hand pin at |eligible| == SLEEVE_N, and same-seed draws DIFFER
   between modes when the sets differ (the null is honest per mode).
"""
from __future__ import annotations

import inspect
from datetime import date

import pytest

from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.backtest.impl_variant_run import (
    FAMILY_XSMOM_500,
    SLEEVE_N,
    TOP_UNIVERSE,
    AdvSelector,
    ImplSleeves,
    impl_family,
    impl_null_results,
    impl_strategy,
)
from atlas.dcp.backtest.portfolio import PanelView, PricePanel
from atlas.dcp.backtest.xsmom_pit_run import KILL_START, run_pit_backtest
from atlas.dcp.signals.pead.v1 import EarningsView
from atlas.dcp.signals.xsmom.v1 import SEASONING
from tests.unit.test_impl_variant import (
    growth_series,
    make_world,
    member,
    weekdays,
)

COSTS = CostModel()


# ------------------------------------------------------- 1. mode semantics ---

def test_full_universe_base_is_the_whole_eligible_set():
    """103 eligible names: the default mode cuts to TOP_UNIVERSE (=100,
    dropping the three smallest traders); top_universe=0 keeps all 103."""
    panel, members, dv = make_world(103)
    view = PanelView(panel, len(panel.dates) - 1)
    full = AdvSelector(members, dv, top_universe=0).base(view)
    assert len(full) == 103
    assert full == tuple(sorted(f"SYM{k:02d}" for k in range(103)))
    screened = AdvSelector(members, dv).base(view)
    assert len(screened) == TOP_UNIVERSE
    assert {"SYM00", "SYM01", "SYM02"}.isdisjoint(screened)


def test_full_universe_reads_no_dollar_volume_at_all():
    """An EMPTY dollar-volume matrix is valid input at top_universe=0 — the
    structural proof that no liquidity data can influence selection. (The
    default mode would assert on the missing series instead.)"""
    panel, members, dv = make_world(12)
    view = PanelView(panel, len(panel.dates) - 1)
    full = AdvSelector(members, {}, top_universe=0).base(view)
    assert full == AdvSelector(members, dv, top_universe=0).base(view)
    assert len(full) == 12
    # cached: same tuple object on re-query (pure property of t)
    sel = AdvSelector(members, {}, top_universe=0)
    assert sel.base(view) is sel.base(view)


# ------------------------------------------------------ 2. default unchanged ---

def test_default_is_the_module_top_universe_and_identical():
    sig = inspect.signature(AdvSelector.__init__)
    assert sig.parameters["top_universe"].default == TOP_UNIVERSE == 100
    panel, members, dv = make_world(103)
    view = PanelView(panel, len(panel.dates) - 1)
    assert (AdvSelector(members, dv).base(view)
            == AdvSelector(members, dv, top_universe=TOP_UNIVERSE).base(view))


# ------------------------------------- 3. the screen used to bind: hand-pin ---

def _inverted_world(n_symbols: int):
    """Momentum winners ARE the ADV losers: SYMkk grows at (n-1-k) bps/day
    (SYM00 fastest) but trades (k+1)*1e6 dollars/day (SYM00 thinnest) — the
    exact name the ADV screen exists to exclude is the top momentum pick."""
    n_sessions = SEASONING + 30
    dates = weekdays(date(2024, 1, 1), n_sessions)
    syms = [f"SYM{k:02d}" for k in range(n_symbols)]
    opens: dict[str, list[float | None]] = {}
    closes: dict[str, list[float | None]] = {}
    dv: dict[str, list[float | None]] = {}
    for k, s in enumerate(syms):
        series = growth_series(n_sessions, 100.0, 0.0001 * (n_symbols - 1 - k))
        opens[s] = list(series)
        closes[s] = list(series)
        dv[s] = [(k + 1) * 1e6] * n_sessions
    panel = PricePanel(dates=dates, opens=opens, closes=closes)
    return panel, {s: member(s) for s in syms}, dv


def test_full_universe_holds_the_screened_out_winners():
    """103 names, momentum reverse to ADV: the default mode's base excludes
    SYM00..SYM02 (thinnest), so its top-5 is SYM03..SYM07; the full-universe
    book holds SYM00..SYM04 — the screened-out extreme tail, exactly the
    portfolio difference ADR-0016 must carry evidence for."""
    panel, members, dv = _inverted_world(103)
    view = PanelView(panel, len(panel.dates) - 1)
    screened = ImplSleeves(AdvSelector(members, dv), EarningsView({}))
    w100 = impl_strategy(screened, "xsmom")(view)
    assert w100 == pytest.approx({f"SYM{k:02d}": 0.2 for k in range(3, 8)})
    full = ImplSleeves(AdvSelector(members, dv, top_universe=0),
                       EarningsView({}))
    w500 = impl_strategy(full, "xsmom")(view)
    assert w500 == pytest.approx({f"SYM{k:02d}": 0.2 for k in range(0, 5)})


# ------------------------------------------------------ 4. family discipline ---

def test_impl500_family_naming_and_kill_sibling():
    assert FAMILY_XSMOM_500 == "xsmom-impl500-tr"
    assert impl_family("xsmom", None, full_universe=True) == "xsmom-impl500-tr"
    assert (impl_family("xsmom", KILL_START, full_universe=True)
            == "xsmom-impl500-tr-2016")
    # without the flag, naming is byte-identical to the validated families
    assert impl_family("xsmom", None) == "xsmom-impl-tr"
    assert impl_family("xsmom", KILL_START) == "xsmom-impl-tr-2016"


@pytest.mark.parametrize("variant", ["pead", "combined"])
def test_impl500_refuses_pead_and_combined(variant):
    """ADR-0015 sets the PEAD sleeve budget to 0 — no impl500 family exists
    for pead/combined, and asking for one is an error, not a silent skip."""
    with pytest.raises(ValueError, match="ADR-0015"):
        impl_family(variant, None, full_universe=True)


# ------------------------------------------------- 5. the monkey stays fair ---

def test_full_universe_monkey_with_exactly_five_eligible_is_the_strategy():
    """|eligible| == SLEEVE_N forces the monkey's hand in full-universe mode
    exactly as in the screened mode: every null path IS the strategy path."""
    panel, members, _ = make_world(5)
    sleeves = ImplSleeves(AdvSelector(members, {}, top_universe=0),
                          EarningsView({}))
    start = panel.dates[0]
    strat = run_pit_backtest(panel, impl_strategy(sleeves, "xsmom"), COSTS,
                             start=start).result
    for r in impl_null_results(panel, sleeves, "xsmom", costs=COSTS,
                               start=start, paths=3, seed=11):
        assert r.equity_curve == pytest.approx(strat.equity_curve)
    assert SLEEVE_N == 5


def test_monkey_draws_differ_between_modes_when_the_sets_differ():
    """Same seed, same world (103 names): the full-universe null samples from
    103 names while the screened null samples from 100 — the draws (and so the
    null distribution) must differ. The null is honest per mode."""
    panel, members, dv = make_world(103)
    kw = dict(costs=COSTS, start=panel.dates[0], paths=4, seed=7)
    screened = ImplSleeves(AdvSelector(members, dv), EarningsView({}))
    full = ImplSleeves(AdvSelector(members, dv, top_universe=0),
                       EarningsView({}))
    a = [r.total_return for r in
         impl_null_results(panel, screened, "xsmom", **kw)]
    b = [r.total_return for r in impl_null_results(panel, full, "xsmom", **kw)]
    assert a != b
    # and each mode remains seed-deterministic
    assert b == [r.total_return for r in
                 impl_null_results(panel, full, "xsmom", **kw)]
