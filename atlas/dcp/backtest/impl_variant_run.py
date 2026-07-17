"""THE IMPLEMENTABLE-VARIANT TEST (board memo item 5; ADR-0010 caveat 3,
ADR-0013): momentum and PEAD were VALIDATED as ~50-name winner deciles on the
point-in-time S&P 500, but the fund TRADES top-5 sleeves (SLEEVE_MAX_NAMES) on
the ~100-name ADR-0007 universe. This runner closes — or honestly fails to
close — that validated-universe vs trading-universe gap by running the LIVE
book shape (top-5 equal weight per sleeve, monthly, 10 bps/side) on an honest
point-in-time large-cap universe, through the IDENTICAL gauntlet the deciles
passed: total-return vs SPY total return (binding, ADR-0009), a 1000-path
monkey null drawing with the SAME top-5 construction from the SAME eligible
set, deflated Sharpe at the TRUE registered family count, purged+embargoed
walk-forward, the verdict-vs-endpoint exhibit, and the board's pre-committed
2016 kill-only trials. Thresholds and machinery are REUSED BY IMPORT from
xsmom_pit_run / pead_pit_run / portfolio_validation — never restated.

UNIVERSE HONESTY (probed 2026-07-18, decided before any backtest ran):
EODHD's OEX.INDX fundamentals carry NO HistoricalTickerComponents — only the
101 current Components — so a true point-in-time S&P 100 CANNOT be built from
the vendor (the GSPC.INDX equivalent that built validation.index_membership
does not exist for OEX). The documented fallback is used instead: the
POINT-IN-TIME S&P 500 (validation.index_membership, fail-closed interval rule,
delisted names included) restricted at EACH REBALANCE to the TOP-100 names by
trailing 63-session average daily dollar volume. This is a deterministic,
point-in-time APPROXIMATION of the S&P 100 (which is committee-chosen with
options-listing and sector-balance criteria we cannot reconstruct); the filter
reads only data <= t, so it introduces no look-ahead, and it is honest about
survivorship — dead mega-caps are eligible while they lived and are liquidated
at their final close when they die (delisting rule imported unchanged).

DOLLAR-VOLUME BASIS (verified against real split rows before use): EODHD
serves bar VOLUME already split-adjusted to the current share basis (AAPL
2020-08-28 volume is stored x4, NVDA 2024-06-07 x10) while stored closes are
raw. True traded dollars on day D are therefore split-adjusted close x STORED
volume — the two adjustment factors cancel. OBar.volume from
load_adjusted_obars is NOT usable for this (adjust_for_splits multiplies the
already-adjusted vendor volume again); stored volumes are re-read raw.

INDIA ADRs are EXCLUDED and documented: the live ADR-0007 universe carries a
5-name India ADR sleeve, but those names hold no US index membership of any
kind, so no honest point-in-time membership exists for them. This variant
therefore tests the US large-cap satellite only; the India sleeve remains
unvalidated by construction.

THREE variants, each a registered trial family, each with a pre-committed
2016 kill-only sibling (six trials per invocation):
  * `xsmom-impl-tr`    — 12-1 momentum (signals/xsmom/v1 recipe by import),
                         top-5 equal weight on the PIT large-cap universe;
  * `pead-impl-tr`     — PEAD/SUE (signals/pead/v1, the corrected
                         no-double-adjust path), top-5 equal weight;
  * `combined-impl-tr` — the actual satellite: 50/50 momentum+PEAD top-5
                         sleeves, rebalanced monthly (ADR-0013 consequence 2).
                         A sleeve with no eligible names holds cash for its
                         half (the live behaviour: no signal, no position).

Do NOT tune anything to pass — a failed gate is a valid, reportable result,
and a FAIL here means the LIVE sleeves rest on an unvalidated extrapolation
(a Principal decision follows; the verdict is the deliverable).

Usage: python -m atlas.dcp.backtest.impl_variant_run [--paths 1000]
           [--seed 7] [--window-end 2026-07-15] [--report PATH]
"""
from __future__ import annotations

import argparse
import random
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.backtest.pead_pit_run import (
    KILL_START as PEAD_KILL_START,
)
from atlas.dcp.backtest.pead_pit_run import (
    PeadCoverage,
    load_pead_signals,
)
from atlas.dcp.backtest.portfolio import (
    PanelView,
    PortfolioResult,
    PortfolioStrategy,
    PricePanel,
    month_end_indices,
)
from atlas.dcp.backtest.portfolio_validation import (
    DSR_MIN,
    P_MAX,
    PortfolioGateReport,
    PortfolioWalkForwardResult,
    buy_and_hold_strategy,
    portfolio_gate,
)
from atlas.dcp.backtest.real_run import (
    COSTS,
    EMBARGO,
    HORIZON,
    K_FOLDS,
    load_adjusted_obars,
)
from atlas.dcp.backtest.registry import register_trial, trial_count
from atlas.dcp.backtest.validation import deflated_sharpe
from atlas.dcp.backtest.xsmom_pit_run import (
    BENCHMARK,
    ENDPOINT_MONTHS,
    KILL_START,
    TR_CONVENTION,
    EndpointVerdict,
    PitBacktest,
    PitUniverse,
    _return_at,
    _sharpe_at,
    load_pit_panel,
    pit_eligible,
    pit_walk_forward,
    run_pit_backtest,
)
from atlas.dcp.backtest.xsmom_run import calendar_year_returns, total_trial_count
from atlas.dcp.market_data.index_membership import (
    INDEX_CODE,
    WINDOW_START,
    MembershipRow,
    is_member_on,
)
from atlas.dcp.signals.pead.generate import SLEEVE_MAX_NAMES as PEAD_SLEEVE_MAX
from atlas.dcp.signals.pead.v1 import EarningsView
from atlas.dcp.signals.xsmom.generate import SLEEVE_MAX_NAMES
from atlas.dcp.signals.xsmom.v1 import LOOKBACK, SEASONING, SKIP

ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "docs" / "reports" / "implementable-variant-2026-07.md"

# The live book shape: top-5 per sleeve (Principal 2026-07-16, imported from
# the production signal generators — the ONE number this test exists to honor).
SLEEVE_N: Final[int] = SLEEVE_MAX_NAMES
if SLEEVE_N != PEAD_SLEEVE_MAX:  # pragma: no cover — both are the signed cap
    raise RuntimeError("xsmom and pead sleeve caps diverge — fix generators")
if KILL_START != PEAD_KILL_START:  # pragma: no cover — one board commitment
    raise RuntimeError("kill-start dates diverge between pit runners")

# Point-in-time large-cap filter (the documented S&P 100 approximation):
# top-100 by trailing 63-session (~one quarter, the STALENESS_SESSIONS
# convention) average daily dollar volume. Both are conventions, not searches.
TOP_UNIVERSE: Final[int] = 100
ADV_WINDOW: Final[int] = 63

FAMILY_XSMOM: Final[str] = "xsmom-impl-tr"
FAMILY_PEAD: Final[str] = "pead-impl-tr"
FAMILY_COMBINED: Final[str] = "combined-impl-tr"
VARIANTS: Final[tuple[str, ...]] = ("xsmom", "pead", "combined")

# The vendor's CURRENT S&P 100 component codes (OEX.INDX `Components`,
# fetched 2026-07-18 during the feasibility probe — the same call that proved
# HistoricalTickerComponents absent). Used ONLY for the report's
# approximation-overlap exhibit; never for selection (it is not point-in-time).
OEX_COMPONENTS_2026_07_18: Final[tuple[str, ...]] = (
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AMAT", "AMD", "AMGN", "AMT",
    "AMZN", "AVGO", "AXP", "BA", "BAC", "BKNG", "BLK", "BMY", "BNY", "BRK-B",
    "C", "CAT", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS",
    "CVX", "DE", "DHR", "DIS", "DUK", "EMR", "FDX", "GD", "GE", "GEV", "GILD",
    "GM", "GOOG", "GOOGL", "GS", "HD", "HONA", "IBM", "INTC", "INTU", "ISRG",
    "JNJ", "JPM", "KO", "LIN", "LLY", "LMT", "LOW", "LRCX", "MA", "MCD",
    "MDLZ", "MDT", "META", "MMM", "MO", "MRK", "MS", "MSFT", "MU", "NEE",
    "NFLX", "NKE", "NOW", "NVDA", "ORCL", "PEP", "PFE", "PG", "PLTR", "PM",
    "QCOM", "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TMO", "TMUS", "TSLA",
    "TXN", "UBER", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WFC", "WMT", "XOM")


def impl_family(variant: str, window_start: date | None) -> str:
    base = {"xsmom": FAMILY_XSMOM, "pead": FAMILY_PEAD,
            "combined": FAMILY_COMBINED}[variant]
    return base if window_start is None else f"{base}-{window_start.year}"


# ---------------------------------------------------------------------------
# Point-in-time large-cap universe: top-TOP_UNIVERSE by trailing dollar volume
# ---------------------------------------------------------------------------

def truncate_panel(panel: PricePanel,
                   window_end: date) -> tuple[PricePanel, tuple[str, ...]]:
    """Cut the panel at window_end (inclusive) so a re-run against a database
    that has since ingested newer bars reproduces this run exactly. Symbols
    whose entire series falls after the cut are dropped (and reported); the
    benchmark must survive."""
    k = bisect_right(panel.dates, window_end)
    if k >= len(panel.dates):
        return panel, ()
    if k < 2:
        raise ValueError(f"window_end {window_end} leaves fewer than two sessions")
    dropped = tuple(sorted(s for s, c in panel.closes.items()
                           if all(x is None for x in c[:k])))
    if BENCHMARK in dropped:
        raise RuntimeError(f"benchmark {BENCHMARK} has no bars before "
                           f"{window_end} — cannot truncate")
    return PricePanel(
        dates=panel.dates[:k],
        opens={s: v[:k] for s, v in panel.opens.items() if s not in dropped},
        closes={s: v[:k] for s, v in panel.closes.items() if s not in dropped},
    ), dropped


def load_dollar_volume(session: Session, symbols: list[str],
                       panel: PricePanel) -> dict[str, list[float | None]]:
    """Panel-aligned true daily dollar volume per symbol: split-adjusted close
    (load_adjusted_obars — the panel's own price basis, BEFORE any total-return
    transform) times the STORED vendor volume (already split-adjusted by the
    vendor, so the split factors cancel and the product is the raw traded
    dollars — see the module docstring's verified basis note)."""
    idx = {d: i for i, d in enumerate(panel.dates)}
    out: dict[str, list[float | None]] = {}
    for sym in symbols:
        obars, ds = load_adjusted_obars(session, sym)
        vols = {r.bar_date: int(r.volume) for r in session.execute(text(
            "SELECT pb.bar_date, pb.volume FROM market.price_bars_daily pb "
            "JOIN market.instruments i ON i.id = pb.instrument_id "
            "WHERE i.symbol = :s AND pb.source = 'EodhdAdapter'"), {"s": sym})}
        dv: list[float | None] = [None] * len(panel.dates)
        for j, d in enumerate(ds):
            i = idx.get(d)
            if i is not None:
                dv[i] = obars[j].close * float(vols[d])
        out[sym] = dv
    return out


class AdvSelector:
    """The point-in-time large-cap filter: at rebalance t, the top-TOP_UNIVERSE
    names by mean dollar volume over sessions [t-ADV_WINDOW+1, t], among the
    point-in-time eligible set (member at t + price at t + SEASONING sessions
    of history — pit_eligible, imported). Eligibility guarantees a contiguous
    series back to t-SEASONING, which dominates ADV_WINDOW, so every window
    session has a dollar-volume observation (asserted). Deterministic
    tie-break (-adv, symbol); results cached per rebalance index — a pure
    property of panel + membership + volumes, shared by strategy, monkey null
    and the equal-weight benchmark by construction."""

    def __init__(self, members: Mapping[str, MembershipRow],
                 dollar_volume: Mapping[str, list[float | None]]) -> None:
        self._members = members
        self._prefix: dict[str, list[float]] = {}
        self._have: dict[str, list[bool]] = {}
        for s, series in dollar_volume.items():
            p = [0.0]
            for x in series:
                p.append(p[-1] + (x if x is not None else 0.0))
            self._prefix[s] = p
            self._have[s] = [x is not None for x in series]
        self._cache: dict[int, tuple[str, ...]] = {}

    def base(self, view: PanelView) -> tuple[str, ...]:
        t = view.t
        got = self._cache.get(t)
        if got is not None:
            return got
        scored: list[tuple[float, str]] = []
        for s in pit_eligible(view, self._members):
            lo = t + 1 - ADV_WINDOW
            assert lo >= 0 and all(self._have[s][lo:t + 1]), \
                f"{s}: dollar-volume gap inside ADV window at t={t}"
            p = self._prefix[s]
            adv = (p[t + 1] - p[lo]) / ADV_WINDOW
            scored.append((-adv, s))
        scored.sort()
        top = tuple(sorted(s for _, s in scored[:TOP_UNIVERSE]))
        self._cache[t] = top
        return top


class ImplSleeves:
    """Per-rebalance eligible sets for the two sleeves, cached. The momentum
    base IS the large-cap universe; the PEAD base is its subset with a live,
    fresh, defined SUE signal (signals/pead/v1 — structural no-look-ahead)."""

    def __init__(self, adv: AdvSelector, earnings: EarningsView) -> None:
        self.adv = adv
        self.earnings = earnings
        self._pead_cache: dict[int, tuple[str, ...]] = {}

    def momentum_base(self, view: PanelView) -> tuple[str, ...]:
        return self.adv.base(view)

    def pead_base(self, view: PanelView) -> tuple[str, ...]:
        t = view.t
        got = self._pead_cache.get(t)
        if got is None:
            got = tuple(s for s in self.adv.base(view)
                        if self.earnings.live(s, t, variant="sue") is not None)
            self._pead_cache[t] = got
        return got

    def halves(self, view: PanelView,
               variant: str) -> list[tuple[tuple[str, ...], float]]:
        """(eligible set, budget) per sleeve: the standalone variants give one
        sleeve the whole equity; the combined satellite splits 50/50 (ADR-0013
        consequence 2). A sleeve's unused budget is cash — never reallocated."""
        if variant == "xsmom":
            return [(self.momentum_base(view), 1.0)]
        if variant == "pead":
            return [(self.pead_base(view), 1.0)]
        if variant == "combined":
            return [(self.momentum_base(view), 0.5),
                    (self.pead_base(view), 0.5)]
        raise ValueError(f"unknown variant {variant!r}")


# ---------------------------------------------------------------------------
# Top-5 sleeve strategies (the LIVE construction), fair monkeys, EW benchmark
# ---------------------------------------------------------------------------

def _momentum_rank(view: PanelView, base: tuple[str, ...]) -> list[str]:
    """The SAME 12-1 recipe as signals.xsmom.v1 (LOOKBACK/SKIP imported,
    identical deterministic tie-break), ranked over the large-cap base."""
    t = view.t
    ranked: list[tuple[float, str]] = []
    for s in base:
        c_form = view.close(s, t - LOOKBACK)
        c_skip = view.close(s, t - SKIP)
        assert c_form is not None and c_skip is not None  # SEASONING == LOOKBACK
        ranked.append((c_skip / c_form - 1.0, s))
    ranked.sort(key=lambda rs: (-rs[0], rs[1]))
    return [s for _, s in ranked]


def _pead_rank(view: PanelView, base: tuple[str, ...],
               earnings: EarningsView) -> list[str]:
    """Live SUE descending (signals/pead/v1 accessor), deterministic tie-break
    — identical to pead_pit_strategy's ordering, on the implementable base."""
    t = view.t
    ranked: list[tuple[float, str]] = []
    for s in base:
        sig = earnings.live(s, t, variant="sue")
        assert sig is not None  # pead_base guarantees it
        ranked.append((sig, s))
    ranked.sort(key=lambda rs: (-rs[0], rs[1]))
    return [s for _, s in ranked]


def _sleeve_rank(view: PanelView, sleeves: ImplSleeves, variant: str,
                 base: tuple[str, ...]) -> list[str]:
    if variant == "xsmom":
        return _momentum_rank(view, base)
    return _pead_rank(view, base, sleeves.earnings)


def impl_strategy(sleeves: ImplSleeves, variant: str) -> PortfolioStrategy:
    """The live book shape: each sleeve holds its TOP-SLEEVE_N ranked names,
    equal weight WITHIN its budget (fewer than SLEEVE_N eligible -> hold them
    all, never pad; none -> that budget is cash). Overlapping names in the
    combined satellite simply sum their sleeve weights (one book, two sleeves
    — exactly how the paper account nets them)."""
    sleeve_kinds = {"xsmom": ("xsmom",), "pead": ("pead",),
                    "combined": ("xsmom", "pead")}[variant]

    def strat(view: PanelView) -> dict[str, float]:
        out: dict[str, float] = {}
        for kind, (base, budget) in zip(sleeve_kinds,
                                        sleeves.halves(view, variant)):
            if not base:
                continue
            top = _sleeve_rank(view, sleeves, kind, base)[:SLEEVE_N]
            w = budget / len(top)
            for s in top:
                out[s] = out.get(s, 0.0) + w
        return out
    return strat


def impl_null_results(panel: PricePanel, sleeves: ImplSleeves, variant: str, *,
                      costs: CostModel, start: date, paths: int,
                      seed: int) -> list[PortfolioResult]:
    """The fair monkey null (ADR-0002 #2) for the top-5 construction: at each
    rebalance every monkey draws min(SLEEVE_N, |eligible|) names uniformly
    without replacement from the IDENTICAL cached eligible set(s) the strategy
    ranks, with the IDENTICAL budgets (combined monkeys draw 5 per sleeve and
    overlaps sum), through the IDENTICAL delisting-aware engine and costs. One
    rng drives all paths sequentially (the validation.py convention). Full
    results are kept so the endpoint exhibit truncates the stored curves
    exactly."""
    rng = random.Random(seed)

    def monkey(view: PanelView) -> dict[str, float]:
        out: dict[str, float] = {}
        for base, budget in sleeves.halves(view, variant):
            if not base:
                continue
            pick = rng.sample(list(base), min(SLEEVE_N, len(base)))
            w = budget / len(pick)
            for s in pick:
                out[s] = out.get(s, 0.0) + w
        return out

    return [run_pit_backtest(panel, monkey, costs, start=start).result
            for _ in range(paths)]


def impl_equal_weight(sleeves: ImplSleeves, variant: str) -> PortfolioStrategy:
    """Informational benchmark (NOT binding): equal weight over each sleeve's
    ENTIRE eligible set within its budget — separates the top-5 selection from
    the large-cap universe tilt."""
    def strat(view: PanelView) -> dict[str, float]:
        out: dict[str, float] = {}
        for base, budget in sleeves.halves(view, variant):
            if not base:
                continue
            w = budget / len(base)
            for s in base:
                out[s] = out.get(s, 0.0) + w
        return out
    return strat


# ---------------------------------------------------------------------------
# Context loading (dev-DB read-only; everything derived, nothing edited)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImplContext:
    universe: PitUniverse            # loader artifact (partition/coverage stats)
    panel: PricePanel                # possibly truncated at window_end
    members: dict[str, MembershipRow]
    sleeves: ImplSleeves
    coverage: PeadCoverage
    dropped_by_truncation: tuple[str, ...]
    window_end: date


def load_impl_context(session: Session, *,
                      window_end: date | None = None) -> ImplContext:
    """One total-return panel for everything (strategy, monkeys, EW, SPY —
    the convention is identical on both sides of every comparison by
    construction, exactly as in the decile runs), the dollar-volume matrix on
    the PRICE basis, and the point-in-time earnings view."""
    universe = load_pit_panel(session, window_end=window_end, total_return=True)
    panel = universe.panel
    dropped: tuple[str, ...] = ()
    if window_end is not None:
        panel, dropped = truncate_panel(panel, window_end)
    members = {s: r for s, r in universe.members.items() if s in panel.closes}
    dollar_volume = load_dollar_volume(session, sorted(members), panel)
    earnings, coverage = load_pead_signals(session, sorted(members),
                                           panel.dates, members)
    return ImplContext(
        universe=universe, panel=panel, members=members,
        sleeves=ImplSleeves(AdvSelector(members, dollar_volume), earnings),
        coverage=coverage, dropped_by_truncation=dropped,
        window_end=panel.dates[-1])


# ---------------------------------------------------------------------------
# Orchestration: one gauntlet per (variant, window), verdicts verbatim
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RebalanceCounts:
    day: date
    members: int          # reconstructed index members that day
    eligible: int         # point-in-time eligible (member + price + seasoning)
    base: int             # after the top-100 dollar-volume filter
    pead_base: int        # base names with a live SUE signal


@dataclass(frozen=True)
class ImplRun:
    variant: str
    family: str
    start: date
    run: PitBacktest
    spy: PortfolioResult
    ew: PortfolioResult
    gate: PortfolioGateReport
    wf: PortfolioWalkForwardResult
    wf_spy: PortfolioWalkForwardResult
    endpoints: tuple[EndpointVerdict, ...]
    trial_id: str
    n_trials: int
    trials_before_total: int
    trials_after_total: int
    counts: tuple[RebalanceCounts, ...]


def _endpoint_verdicts(strategy: PortfolioResult, spy: PortfolioResult,
                       nulls: list[PortfolioResult],
                       n_trials: int) -> tuple[EndpointVerdict, ...]:
    """The board's endpoint exhibit, EXACT (a curve truncated at endpoint E
    equals a run ended at E — decisions at t read only data <= t and a pending
    trade executes after the truncation mark). Thresholds imported; same
    strictly-beats-SPY rule as portfolio_gate."""
    dates = strategy.dates
    if spy.dates != dates or any(r.dates != dates for r in nulls):
        raise RuntimeError("strategy/SPY/null curves cover different sessions "
                           "— the shared-window invariant is broken")
    month_ends = [i for i in range(len(dates) - 1)
                  if dates[i].month != dates[i + 1].month]
    endpoints = month_ends[-ENDPOINT_MONTHS:] + [len(dates) - 1]
    out: list[EndpointVerdict] = []
    for idx in endpoints:
        sr = _return_at(strategy, idx)
        spy_r = _return_at(spy, idx)
        p = sum(1 for nr in nulls if _return_at(nr, idx) >= sr) / len(nulls)
        dsr = deflated_sharpe(_sharpe_at(strategy, idx), idx, n_trials)
        beats = sr > spy_r
        out.append(EndpointVerdict(
            endpoint=dates[idx], strategy_return=sr, spy_return=spy_r,
            null_p=p, dsr=dsr, beats_spy=beats,
            passed=beats and p <= P_MAX and dsr >= DSR_MIN))
    return tuple(out)


def run_impl_variant(session: Session, audit: PostgresAuditLog,
                     ctx: ImplContext, *, variant: str, paths: int = 1000,
                     seed: int = 7, window_start: date | None = None) -> ImplRun:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    if window_start is not None and window_start <= WINDOW_START:
        raise ValueError(f"window_start {window_start} must be after the "
                         f"membership-reliability bound {WINDOW_START}")
    family = impl_family(variant, window_start)
    panel, sleeves = ctx.panel, ctx.sleeves
    eval_start = WINDOW_START if window_start is None else window_start
    start_i = bisect_left(panel.dates, eval_start)
    if start_i >= len(panel.dates):
        raise RuntimeError(f"panel ends before the evaluation start {eval_start}")
    if start_i < SEASONING:
        raise RuntimeError(f"only {start_i} sessions precede {eval_start} — "
                           f"the first rebalance needs {SEASONING} sessions")
    start = panel.dates[start_i]
    strategy = impl_strategy(sleeves, variant)

    pit = run_pit_backtest(panel, strategy, COSTS, start=start)
    result = pit.result

    counts: list[RebalanceCounts] = []
    for t in month_end_indices(panel.dates, start_i, len(panel.dates)):
        day = panel.dates[t]
        view = PanelView(panel, t)
        counts.append(RebalanceCounts(
            day=day,
            members=sum(1 for r in ctx.universe.window_rows
                        if is_member_on(r, day)),
            eligible=len(pit_eligible(view, ctx.members)),
            base=len(sleeves.momentum_base(view)),
            pead_base=len(sleeves.pead_base(view))))

    trials_before_total = total_trial_count(session)
    spec: dict[str, object] = {
        "family": family, "variant": variant, "version": "1.0.0",
        "universe": f"point-in-time {INDEX_CODE} membership "
                    "(validation.index_membership, fail-closed interval rule) "
                    f"restricted per rebalance to the TOP-{TOP_UNIVERSE} by "
                    f"trailing {ADV_WINDOW}-session mean dollar volume "
                    "(split-adjusted close x vendor split-adjusted volume; "
                    "S&P 100 approximation — OEX.INDX serves no "
                    "HistoricalTickerComponents, probed 2026-07-18)",
        "signals": {"xsmom": "12-1 formation (signals/xsmom/v1: LOOKBACK=252, "
                             "SKIP=21), imported",
                    "pead": "SUE (signals/pead/v1, corrected no-double-adjust "
                            "path), imported"},
        "construction": f"top-{SLEEVE_N} equal weight per sleeve "
                        "(SLEEVE_MAX_NAMES, the live cap); combined = 50/50 "
                        "momentum+PEAD sleeves, overlap sums, empty sleeve "
                        "holds cash",
        "return_convention": TR_CONVENTION,
        "india_adrs": "excluded — no US index membership exists for them; "
                      "this variant tests the US large-cap satellite only",
        "window_start": str(WINDOW_START), "evaluation_start": str(eval_start),
        "window": f"{panel.dates[0]}..{panel.dates[-1]}", "start": str(start),
        "rebalance": "monthly", "weighting": "equal",
        "delisting_rule": "liquidate at final available close, per-side cost, "
                          "proceeds in cash until next rebalance",
        "data": "EODHD real",
        "costs_bps_per_side": COSTS.commission_bps + COSTS.slippage_bps}
    trial_id = register_trial(
        session, family=family, spec=spec,
        metrics={"total_return": result.total_return, "sharpe": result.sharpe,
                 "max_drawdown": result.max_drawdown,
                 "avg_turnover": result.avg_turnover,
                 "n_rebalances": float(result.n_rebalances)})
    n_trials = trial_count(session, family)
    trials_after_total = total_trial_count(session)

    nulls = impl_null_results(panel, sleeves, variant, costs=COSTS,
                              start=start, paths=paths, seed=seed)
    spy = run_pit_backtest(panel, buy_and_hold_strategy(BENCHMARK), COSTS,
                           start=start).result
    ew = run_pit_backtest(panel, impl_equal_weight(sleeves, variant), COSTS,
                          start=start).result
    gate = portfolio_gate(result=result,
                          null_returns=[r.total_return for r in nulls],
                          spy=spy, ew=ew, n_trials=n_trials)
    endpoints = _endpoint_verdicts(result, spy, nulls, n_trials)
    del nulls  # curves served the exhibit; free ~30MB per gauntlet
    wf = pit_walk_forward(panel, strategy, k=K_FOLDS, horizon=HORIZON,
                          embargo=EMBARGO, warmup=start_i, costs=COSTS)
    wf_spy = pit_walk_forward(panel, buy_and_hold_strategy(BENCHMARK),
                              k=K_FOLDS, horizon=HORIZON, embargo=EMBARGO,
                              warmup=start_i, costs=COSTS)

    audit.append(
        event_type="quant.backtest.completed", entity_type="strategy",
        entity_id=f"{family}/portfolio", actor_type="dcp",
        actor_id="impl_variant_run",
        payload={"universe": f"point-in-time {INDEX_CODE} top-{TOP_UNIVERSE} "
                             f"by {ADV_WINDOW}-session dollar volume "
                             "(S&P 100 approximation; OEX history unavailable)",
                 "variant": variant, "sleeve_n": SLEEVE_N,
                 "return_convention": TR_CONVENTION,
                 "trial_id": trial_id, "n_trials": n_trials,
                 "window": f"{panel.dates[0]}..{panel.dates[-1]}",
                 "start": str(start), "gate_passed": gate.passed,
                 "gate_reasons": list(gate.reasons),
                 "null_p": gate.null_p_value, "dsr": gate.dsr,
                 "spy_bh_return": gate.spy_bh_return,
                 "ew_return": gate.ew_return,
                 "forced_liquidations": len(pit.forced_liquidations),
                 "unfilled_buys": len(pit.unfilled_buys),
                 "avg_turnover": result.avg_turnover,
                 "n_rebalances": result.n_rebalances,
                 "wf_positive_folds": wf.positive_folds,
                 "endpoints_beat_spy": sum(1 for e in endpoints if e.beats_spy),
                 "endpoints_pass": sum(1 for e in endpoints if e.passed),
                 "endpoints_total": len(endpoints)})
    return ImplRun(variant=variant, family=family, start=start, run=pit,
                   spy=spy, ew=ew, gate=gate, wf=wf, wf_spy=wf_spy,
                   endpoints=endpoints, trial_id=trial_id, n_trials=n_trials,
                   trials_before_total=trials_before_total,
                   trials_after_total=trials_after_total,
                   counts=tuple(counts))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_LABELS: Final[dict[str, str]] = {
    "xsmom": f"momentum 12-1, top-{SLEEVE_N} sleeve",
    "pead": f"PEAD/SUE, top-{SLEEVE_N} sleeve",
    "combined": f"the actual satellite: 50/50 momentum+PEAD top-{SLEEVE_N} sleeves",
}


def _run_lines(run: ImplRun, title: str) -> list[str]:
    g, wf, r = run.gate, run.wf, run.run.result
    verdict = "PASS" if g.passed else "FAIL"
    n_beat = sum(1 for e in run.endpoints if e.beats_spy)
    n_pass = sum(1 for e in run.endpoints if e.passed)
    lines = [
        f"## {title}",
        "",
        f"Family `{run.family}`; evaluation start {run.start}; "
        f"{r.n_rebalances} rebalances; forced delisting liquidations "
        f"{len(run.run.forced_liquidations)}; unfilled buys "
        f"{len(run.run.unfilled_buys)}.",
        "",
        f"Return {r.total_return:+.2%}, Sharpe {r.sharpe:.2f}, max drawdown "
        f"{r.max_drawdown:.2%}, avg turnover {r.avg_turnover:.2%} per "
        "rebalance (sum |Δw|, both sides)",
        "",
        f"### Gate verdict: **{verdict}**",
        "",
        f"- verdict: **{verdict}**",
        f"- strategy TOTAL return: {g.strategy_return:+.2%}",
        f"- SPY buy-and-hold TOTAL return (BINDING benchmark per ADR-0009): "
        f"{g.spy_bh_return:+.2%}",
        f"- margin over SPY TR: {g.strategy_return - g.spy_bh_return:+.2%}",
        f"- equal-weight whole-eligible-base TR (informational, NOT binding): "
        f"{g.ew_return:+.2%}",
        f"- null-model p-value: {g.null_p_value:.3f} (must be <= {P_MAX}) — "
        f"monkeys draw {SLEEVE_N} names from the identical eligible set with "
        "the identical construction",
        f"- deflated Sharpe: {g.dsr:.3f} at n_trials={g.n_trials} "
        f"(must be >= {DSR_MIN})",
        f"- trial registry id: `{run.trial_id}`",
        "",
    ]
    if g.reasons:
        lines.append("Verbatim gate reasons:")
        lines += [f"- {reason}" for reason in g.reasons]
        lines.append("")
    lines += [
        f"### Walk-forward: {wf.positive_folds}/{len(wf.fold_results)} folds "
        "positive — with SPY through the identical fold machinery",
        "",
        "| fold | strategy TR | SPY TR (same fold) | strategy − SPY |",
        "|---|---|---|---|",
        *[f"| {i} | {fr.total_return:+.2%} | {sp.total_return:+.2%} "
          f"| {fr.total_return - sp.total_return:+.2%} |"
          for i, (fr, sp) in enumerate(
              zip(wf.fold_results, run.wf_spy.fold_results), start=1)],
        "",
        f"- mean return {wf.mean_return:+.2%}, mean Sharpe "
        f"{wf.mean_sharpe:.2f}, worst fold {wf.worst_fold_return:+.2%}",
        "",
        f"### Exhibit: verdict vs endpoint — {title}",
        "",
        f"**{n_beat}/{len(run.endpoints)} endpoints beat SPY TR; "
        f"{n_pass}/{len(run.endpoints)} endpoints PASS the full gate.** "
        "(final date rolled back to each of the prior "
        f"{ENDPOINT_MONTHS} month-ends; exact truncation of the stored "
        "strategy/SPY/null curves)",
        "",
        "| endpoint | strategy TR | SPY TR | margin | null p | DSR | verdict |",
        "|---|---|---|---|---|---|---|",
        *[f"| {e.endpoint} | {e.strategy_return:+.2%} | {e.spy_return:+.2%} "
          f"| {e.strategy_return - e.spy_return:+.2%} | {e.null_p:.3f} "
          f"| {e.dsr:.3f} | {'PASS' if e.passed else 'FAIL'} |"
          for e in run.endpoints],
        "",
    ]
    return lines


def _per_year_lines(runs: Mapping[str, ImplRun]) -> list[str]:
    full = runs["xsmom"]
    spy_years = {y.year: y for y in calendar_year_returns(full.spy)}
    cols = {v: {y.year: y for y in calendar_year_returns(runs[v].run.result)}
            for v in VARIANTS}
    lines = [
        "### Exhibit: per-calendar-year total returns (full-window variants "
        "vs SPY TR)",
        "",
        "| year | xsmom-impl | pead-impl | combined-impl | SPY TR |",
        "|---|---|---|---|---|",
    ]
    for year in sorted(spy_years):
        cells = " | ".join(f"{cols[v][year].ret:+.2%}" if year in cols[v]
                           else "—" for v in VARIANTS)
        lines.append(f"| {year} | {cells} | {spy_years[year].ret:+.2%} |")
    lines.append("")
    return lines


def render_impl_report(ctx: ImplContext, runs: Mapping[str, ImplRun],
                       kills: Mapping[str, ImplRun], *, paths: int) -> str:
    panel = ctx.panel
    tr = ctx.universe.tr
    assert tr is not None
    full = runs["xsmom"]
    last_reb = full.counts[-1]
    # approximation-overlap exhibit: final-rebalance base vs current OEX list
    last_t = month_end_indices(panel.dates, panel.index_at(full.start),
                               len(panel.dates))[-1]
    final_base = set(ctx.sleeves.momentum_base(PanelView(panel, last_t)))
    oex_overlap = len(final_base & set(OEX_COMPONENTS_2026_07_18))
    decision_grade = (panel.dates[-1] - full.start).days >= 3650

    def verdict(r: ImplRun) -> str:
        return "PASS" if r.gate.passed else "FAIL"

    lines = [
        "# THE IMPLEMENTABLE-VARIANT TEST — the live top-5 sleeves on an "
        "honest point-in-time large-cap universe (2026-07)",
        "",
        "> ## WHY THIS TEST EXISTS (board item 5 — the OPEN obligation of "
        "ADR-0010/0013)",
        "> Momentum (`xsmom-pit-tr`) and PEAD (`pead-sue-tr`) were VALIDATED "
        "as ~50-name winner",
        "> deciles on the point-in-time S&P 500. The fund TRADES top-5 "
        "sleeves on the ~100-name",
        "> ADR-0007 universe. ADR-0010 caveat 3 records this "
        "validated-universe vs trading-universe",
        "> gap and names this backtest the next quant deliverable. The "
        "variants below run the LIVE",
        "> book shape through the IDENTICAL gauntlet the deciles passed — "
        "verdicts land verbatim,",
        "> pass or fail.",
        "",
        *(["> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)",
           f"> Evaluation window {full.start} → {panel.dates[-1]} (>= 10 "
           "years); verdicts are decision-grade",
           "> FOR THE IMPLEMENTABILITY QUESTION — pass or fail, recorded "
           "verbatim.",
           ""] if decision_grade else
          ["> ## ⚠️ SMALL-SAMPLE WARNING (ADR-0004)",
           "> Short window; verdicts are **not decision-grade**.",
           ""]),
        "## Universe construction — the honesty section (read first)",
        "",
        "### The point-in-time S&P 100 does NOT exist at the vendor",
        "",
        "Probed live 2026-07-18: `fundamentals/OEX.INDX` returns only "
        "`General` and the 101",
        "current `Components` — **no `HistoricalTickerComponents`** (the "
        "GSPC.INDX table that",
        "built `validation.index_membership` has no OEX equivalent). A true "
        "point-in-time",
        "S&P 100 therefore CANNOT be reconstructed from our vendor, and no "
        "third-party list",
        "was fabricated in its place.",
        "",
        "### The documented fallback (an APPROXIMATION, and why it is honest)",
        "",
        "The point-in-time S&P 500 (fail-closed interval rule, delisted "
        "names included —",
        f"the `{INDEX_CODE}` membership the decile runs validated on) "
        f"restricted at EACH",
        f"rebalance to the **top-{TOP_UNIVERSE} names by trailing "
        f"{ADV_WINDOW}-session mean daily dollar",
        "volume**. Properties:",
        "",
        "- **Point-in-time**: the filter at rebalance t reads only sessions "
        "<= t (prefix sums",
        "  over the panel; asserted in code). Dead mega-caps are eligible "
        "while they lived;",
        "  a held name that dies is liquidated at its final close (delisting "
        "rule imported",
        "  unchanged).",
        "- **Deterministic**: tie-break (-dollar volume, symbol); cached per "
        "rebalance; the",
        "  strategy, the monkey null and the equal-weight benchmark all read "
        "the identical set.",
        "- **Dollar-volume basis (verified before use)**: EODHD stores bar "
        "volume already",
        "  split-adjusted (AAPL 2020-08-28 stored x4, NVDA 2024-06-07 "
        "x10) while closes are",
        "  raw; true traded dollars = split-adjusted close x stored volume "
        "(the factors",
        "  cancel). The engine's OBar volume is double-adjusted and was NOT "
        "used.",
        "- **It is an approximation**: the real S&P 100 is committee-chosen "
        "(options listing,",
        "  sector balance) and cannot be reconstructed point-in-time. "
        "Cross-check where a",
        f"  check exists: at the final rebalance ({panel.dates[last_t]}) the "
        f"filter's top-{TOP_UNIVERSE}",
        f"  overlaps the vendor's CURRENT S&P 100 components on "
        f"**{oex_overlap}/{len(OEX_COMPONENTS_2026_07_18)}** names.",
        "  Historical overlap is unverifiable (that is exactly the missing "
        "data); this is",
        "  stated, not hidden.",
        "- **India ADRs are excluded**: the live ADR-0007 universe carries 5 "
        "India ADRs, but",
        "  they hold no US index membership, so no honest point-in-time "
        "construction covers",
        "  them. **This test validates (or fails) the US large-cap satellite "
        "only; the India",
        "  sleeve remains unvalidated by construction.**",
        "",
        "### Panel and coverage (inherited from the decile runs' loader, "
        "unchanged)",
        "",
        f"- Panel {panel.dates[0]} → {panel.dates[-1]} "
        f"({len(panel.dates)} aligned XNYS sessions), total-return "
        "convention on every",
        "  series (dividends reinvested at the ex-date close; identical on "
        "both sides of",
        "  every comparison)",
        f"- Members with usable series: {len(ctx.members)} "
        f"({ctx.universe.included_delisted} delisted); missing series: "
        f"{len(ctx.universe.missing_series)}; SPY carries "
        f"{tr.spy_dividends} reinvested distributions (asserted non-zero)",
        f"- Earnings coverage: {ctx.coverage.symbols_with_reports} members "
        f"with >= 1 stored surprise ({ctx.coverage.total_reports} reports; "
        f"{ctx.coverage.delisted_with_reports} delisted names)",
        f"- Rebalance-set sizes at the final rebalance ({last_reb.day}): "
        f"{last_reb.members} members / {last_reb.eligible} eligible / "
        f"{last_reb.base} large-cap base / {last_reb.pead_base} with live SUE",
        "- December snapshots (members/eligible/base/PEAD-base): "
        + "; ".join(f"{c.day.year}: {c.members}/{c.eligible}/{c.base}/"
                    f"{c.pead_base}"
                    for c in full.counts if c.day.month == 12),
        "",
        "## Method (everything imported, nothing restated)",
        "",
        f"- Construction: top-{SLEEVE_N} equal weight per sleeve "
        "(SLEEVE_MAX_NAMES — the live cap, imported from the production "
        "signal generators); monthly rebalance at month-end close, execution "
        f"next session's open; costs {COSTS.commission_bps:.0f}+"
        f"{COSTS.slippage_bps:.0f} bps/side on turnover",
        "- Combined satellite: 50/50 momentum+PEAD sleeves (ADR-0013 "
        "consequence 2), overlap sums, an empty sleeve holds cash",
        f"- Null model: {paths}-path monkey MC, min({SLEEVE_N}, |eligible|) "
        "names drawn uniformly from the SAME cached eligible set(s) with the "
        "SAME budgets, identical engine/costs/delisting rule (ADR-0002 #2)",
        f"- Walk-forward: purged+embargoed, k={K_FOLDS}, horizon={HORIZON}, "
        f"embargo={EMBARGO}, warmup = evaluation-start index (ADR-0002 #3)",
        "- Deflated Sharpe at each family's true registered trial count "
        "(ADR-0002 #1); every run registered in quant.trial_registry",
        "- Binding benchmark: SPY buy-and-hold TOTAL return over the same "
        "window (ADR-0009); SPY holds no membership row and can never be "
        "ranked",
        f"- Pre-committed kill-only trials: evaluation start {KILL_START} "
        "(imported from the decile runs' board commitment) — they can only "
        "demote, never validate",
        "",
        *_run_lines(runs["xsmom"],
                    f"Variant 1 — `{runs['xsmom'].family}`: "
                    + _LABELS["xsmom"]),
        *_run_lines(runs["pead"],
                    f"Variant 2 — `{runs['pead'].family}`: " + _LABELS["pead"]),
        *_run_lines(runs["combined"],
                    f"Variant 3 — `{runs['combined'].family}`: "
                    + _LABELS["combined"]),
        "# Pre-committed 2016 kill-only trials (demote-only)",
        "",
        "Identical recipes, evaluation start "
        f"{KILL_START}: they remove the biased early-membership window and "
        "the 2012-2015 head start. A PASS validates nothing by itself; a "
        "FAIL is a strike.",
        "",
        *_run_lines(kills["xsmom"],
                    f"Kill 1 — `{kills['xsmom'].family}`: " + _LABELS["xsmom"]),
        *_run_lines(kills["pead"],
                    f"Kill 2 — `{kills['pead'].family}`: " + _LABELS["pead"]),
        *_run_lines(kills["combined"],
                    f"Kill 3 — `{kills['combined'].family}`: "
                    + _LABELS["combined"]),
        *_per_year_lines(runs),
        "## Summary",
        "",
        "| trial | window | strategy TR | SPY TR | margin | null p | DSR (n) "
        "| WF+ | endpoints beat/pass | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in [runs[v] for v in VARIANTS] + [kills[v] for v in VARIANTS]:
        g = r.gate
        lines.append(
            f"| `{r.family}` | {r.start} → {panel.dates[-1]} "
            f"| {g.strategy_return:+.2%} | {g.spy_bh_return:+.2%} "
            f"| {g.strategy_return - g.spy_bh_return:+.2%} "
            f"| {g.null_p_value:.3f} | {g.dsr:.3f} ({g.n_trials}) "
            f"| {r.wf.positive_folds}/{len(r.wf.fold_results)} "
            f"| {sum(1 for e in r.endpoints if e.beats_spy)}/"
            f"{sum(1 for e in r.endpoints if e.passed)}"
            f"/{len(r.endpoints)} | **{verdict(r)}** |")
    first_before = runs["xsmom"].trials_before_total
    last_after = kills["combined"].trials_after_total
    lines += [
        "",
        f"Trial registry: **{first_before} trials before this run → "
        f"{last_after} after** (six trials: three full-window families, "
        "three pre-committed kills).",
        "",
        "## What this means for the LIVE sleeves",
        "",
    ]
    any_pass = any(runs[v].gate.passed for v in VARIANTS)
    combined_pass = runs["combined"].gate.passed
    if combined_pass:
        lines += [
            "The combined-satellite variant — the closest construction to "
            "what the fund actually holds — CLEARED the binding bar on the "
            "honest point-in-time large-cap universe. The "
            "validated-universe vs trading-universe gap of ADR-0010 caveat 3 "
            "is closed to the extent this approximation allows (the "
            "S&P 100 approximation and the excluded India sleeve are the "
            "remaining daylight, both documented above). Per-variant "
            "verdicts above stand on their own.",
        ]
        for v in ("xsmom", "pead"):
            r = runs[v]
            if r.gate.passed:
                continue
            lines += [
                "",
                f"**However: the standalone `{r.family}` sleeve FAILED its "
                f"own gate** (null p={r.gate.null_p_value:.3f}; "
                f"{sum(1 for e in r.endpoints if e.beats_spy)}/"
                f"{len(r.endpoints)} endpoints beat SPY TR). At "
                f"top-{SLEEVE_N} concentration its ranking is "
                "indistinguishable from drawing names at random from the "
                "same eligible set — the combined PASS is carried by the "
                "other sleeve, not by this one. Whether this sleeve keeps "
                "its own live budget is a PRINCIPAL DECISION on this "
                "evidence: its decile validation does not transfer to the "
                "live book shape on its own.",
            ]
    elif any_pass:
        lines += [
            "MIXED verdicts: at least one implementable variant clears the "
            "bar but the combined satellite — the construction the fund "
            "actually holds — does not (or vice versa; the table above is "
            "authoritative). The live sleeves' fate is a PRINCIPAL DECISION: "
            "the paper book currently trades a construction whose own "
            "backtest evidence is split. Options on the table: reshape the "
            "sleeves toward the variant that passed, suspend the failing "
            "sleeve(s), or continue paper trading with the gap recorded as "
            "accepted risk in a signed ADR amendment.",
        ]
    else:
        lines += [
            "ALL implementable variants FAILED the binding bar. The decile "
            "validations (`xsmom-pit-tr`, `pead-sue-tr`) do NOT transfer to "
            "the book the fund actually trades: the live top-5 sleeves rest "
            "on an UNVALIDATED EXTRAPOLATION — the edge lives in the breadth "
            "of the ~50-name decile and/or the full-500 universe, not in a "
            "5-name concentrated sleeve on a 100-name large-cap set. This "
            "verdict is the deliverable. A PRINCIPAL DECISION follows: "
            "suspend the paper sleeves, reshape them toward the validated "
            "decile construction (which at A$100k NAV was rejected as "
            "sub-minimum — the original reason for the top-5 cap), or accept "
            "and sign the extrapolation risk in an ADR. Nothing here demotes "
            "automatically: the tolerance bands of ADR-0010/0013 remain the "
            "operating tripwires.",
        ]
    lines += [
        "",
        "Caveats that survive any verdict: (1) the universe is an "
        "approximation of the S&P 100, documented above; (2) the India ADR "
        "sleeve is untested by construction; (3) the early-window membership "
        "undercount that flattered the decile runs flatters these runs "
        "identically; (4) endpoint concentration must be read from the "
        "exhibits, not assumed away; (5) the board-memo item-5 overlay "
        "(ADR-0006 2xATR stops, L9 staggered entries, L5 gross caps, "
        "small-account frictions beyond 10 bps/side) is NOT modeled — this "
        "run isolates the universe + top-5 concentration question, and the "
        "stop-overlaid configuration still has no backtest evidence of its "
        "own.",
        "",
        "## Reproduction",
        "",
        "Deterministic re-run (official registration against the dev "
        "database, after review):",
        "",
        "```bash",
        f"python -m atlas.dcp.backtest.impl_variant_run --paths {paths} "
        f"--seed 7 --window-end {panel.dates[-1]}",
        "```",
        "",
        "The `--window-end` pin makes the run byte-identical even after "
        "later nightly ingests extend the stored history.",
        "",
        "## Approval status",
        "",
        "**None sought here — by design.** This is a VALIDATION run on the "
        "membership-gated universe (validation-only instruments); it does "
        "not qualify or disqualify any strategy row by itself. Gates were "
        "not modified; verdicts are recorded verbatim; what happens to the "
        "live sleeves is a Principal decision made on this evidence.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(
        description="Implementable-variant backtest: live top-5 sleeves on "
                    "the point-in-time large-cap universe (board item 5)")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--window-end", type=date.fromisoformat, default=None,
                   help="pin the panel's final session (reproducibility "
                        "across later ingests); default = last stored bar")
    p.add_argument("--report", type=Path, default=None)
    a = p.parse_args()
    report_path: Path = a.report or REPORT

    with session_scope() as s:
        last_bar = s.execute(text(
            "SELECT max(bar_date) FROM market.price_bars_daily "
            "WHERE source='EodhdAdapter'")).scalar()
        if last_bar is None:
            raise SystemExit("no real bars in the database — run the backfill "
                             "first")
        clock = FrozenClock(datetime(last_bar.year, last_bar.month,
                                     last_bar.day, 22, 0, tzinfo=UTC))
        audit = PostgresAuditLog(s, clock)
        ctx = load_impl_context(s, window_end=a.window_end)
        runs: dict[str, ImplRun] = {}
        kills: dict[str, ImplRun] = {}
        for variant in VARIANTS:
            runs[variant] = run_impl_variant(s, audit, ctx, variant=variant,
                                             paths=a.paths, seed=a.seed)
            kills[variant] = run_impl_variant(s, audit, ctx, variant=variant,
                                              paths=a.paths, seed=a.seed,
                                              window_start=KILL_START)
            for r in (runs[variant], kills[variant]):
                g = r.gate
                print(f"{r.family}: gate={'PASS' if g.passed else 'FAIL'} "
                      f"return={g.strategy_return:+.2%} "
                      f"spy={g.spy_bh_return:+.2%} "
                      f"margin={g.strategy_return - g.spy_bh_return:+.2%} "
                      f"p={g.null_p_value:.3f} dsr={g.dsr:.3f} "
                      f"wf={r.wf.positive_folds}/{len(r.wf.fold_results)} "
                      f"endpoints={sum(1 for e in r.endpoints if e.beats_spy)}"
                      f"/{sum(1 for e in r.endpoints if e.passed)}"
                      f"/{len(r.endpoints)} "
                      f"(reasons: {list(g.reasons) or 'none'})", flush=True)

    report = render_impl_report(ctx, runs, kills, paths=a.paths)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    print(f"trials: {runs['xsmom'].trials_before_total} -> "
          f"{kills['combined'].trials_after_total}")
    print(f"report written: {report_path}")


if __name__ == "__main__":
    main()
