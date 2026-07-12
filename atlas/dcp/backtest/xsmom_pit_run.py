"""THE DEFINITIVE MOMENTUM TEST: cross-sectional momentum (the same textbook
Jegadeesh-Titman 12-1 recipe as signals.xsmom.v1, zero sweeps) on the
POINT-IN-TIME S&P 500 — membership as it stood at each rebalance, INCLUDING
companies that later died. This settles the survivorship question the S&P-100
run (conditional PASS) and the sector-ETF cross-check (FAIL) left open.

Membership: validation.index_membership (migration 0015), reconstructed under
the fail-closed interval rule owned by market_data/index_membership.py — a
ticker is a member on day D iff (start IS NULL OR start <= D) AND (end IS NULL
OR end > D), null StartDates usable ONLY for current members, everything else
excluded and counted. EndDates are sparse before ~2012, so the evaluation
window STARTS 2012-07-01 (WINDOW_START there); membership before that is
unreliable and this runner will not evaluate it.

Eligibility at rebalance t: member-at-t AND >= SEASONING (252) prior sessions
of stored data AND a price at t. Winners: the TOP DECILE of the eligible set
(n_eligible // 10, floored at v1's TOP_N=10), equal weight — the J&T
construction is fractional (the winner decile of the ranked universe), and on
a ~300-500-name eligible set the decile is 30-50 names.

DELISTING RULE (the honest terminal-value convention, hand-pinned by test): a
held name whose series ends mid-hold is liquidated at its FINAL AVAILABLE
CLOSE — the position converts to cash at its last mark, paying the same
per-side cost as any other sell, and the proceeds sit in cash until the next
rebalance. A pending buy whose name dies between decision close and execution
open simply does not fill (that weight stays in cash). The engine here mirrors
the frozen portfolio.py accounting move-for-move (equivalence pinned by test
on a delisting-free panel; portfolio.py itself is untouched) and adds ONLY
those two delisting behaviours — the frozen engine refuses mid-hold missing
prices by design, so dead names need this sibling.

Monkey null (ADR-0002 #2): 1000 seeded paths drawing the SAME COUNT of names
uniformly from the SAME point-in-time eligible set at each rebalance,
identical engine, costs and delisting rule. Gate thresholds are IMPORTED from
portfolio_validation (never restated); deflated Sharpe at the true registry
count (ONE trial, family 'xsmom-pit'); purged walk-forward per the ETF-run
convention with warmup = the evaluation-window start index (which dominates
SEASONING and keeps every fold inside the membership-reliable window). The
BINDING benchmark is SPY buy-and-hold over the same window (ADR-0009);
equal-weight-all-eligible is shown for information. SPY rides in the panel for
axis identity but holds no membership row, so it can never enter the ranked
universe (asserted).

Convention note (inherited from the round-2 machinery, applied identically to
strategy, null and both benchmarks): bars are split-adjusted PRICE returns —
dividends are not reinvested on either side of the comparison.

Do NOT tune anything to pass — a failed gate is a valid, reportable result.

Usage: python -m atlas.dcp.backtest.xsmom_pit_run [--paths 1000]
"""
from __future__ import annotations

import argparse
import random
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.engine import CostModel
from atlas.dcp.backtest.portfolio import (
    PanelView,
    PortfolioResult,
    PortfolioStrategy,
    PricePanel,
    _drift,
    _validated_targets,
    month_end_indices,
    turnover,
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
from atlas.dcp.backtest.walkforward import leakage_free, purged_folds
from atlas.dcp.backtest.xsmom_run import (
    _PCTS,
    BOOT_BLOCK,
    BOOT_DRAWS,
    BOOT_HORIZON,
    BOOT_SEED,
    block_bootstrap_annual,
    calendar_year_returns,
    daily_returns,
    percentile,
    total_trial_count,
)
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.market_data.index_membership import (
    INDEX_CODE,
    WINDOW_START,
    MembershipPartition,
    MembershipRow,
    is_member_on,
    load_membership,
    member_in_window,
    partition_membership,
)
from atlas.dcp.signals.xsmom.v1 import LOOKBACK, SEASONING, SKIP, SPEC, TOP_N

ROOT = Path(__file__).resolve().parents[3]
BENCHMARK = "SPY"
FAMILY = "xsmom-pit"
DECILE = 10  # J&T winner fraction: the top tenth of the ranked universe


def winner_count(n_eligible: int) -> int:
    """Winner-decile size: n_eligible // DECILE, floored at v1's TOP_N (=10).
    The floor keeps thin early months diversified instead of concentrating
    into fewer than the v1 winner-portfolio size; when fewer than TOP_N names
    are eligible at all, the strategy simply holds them all (never pads)."""
    return max(TOP_N, n_eligible // DECILE)


# ---------------------------------------------------------------------------
# Point-in-time eligibility (shared verbatim by strategy, monkey null and the
# equal-weight benchmark, so all face the identical universe by construction)
# ---------------------------------------------------------------------------

def pit_eligible(view: PanelView, members: Mapping[str, MembershipRow]) -> list[str]:
    """Symbols that are index members on the view's date (fail-closed interval
    rule) AND have a price at t AND >= SEASONING prior sessions of stored data
    (under the panel's contiguity invariant, a close at t - SEASONING proves
    exactly that history — same proof as v1's eligible_symbols)."""
    t, today = view.t, view.today
    out: list[str] = []
    for s in view.symbols():
        row = members.get(s)
        if row is None or not is_member_on(row, today):
            continue
        if view.close(s, t) is None or view.close(s, t - SEASONING) is None:
            continue
        out.append(s)
    return out


def xsmom_pit_strategy(members: Mapping[str, MembershipRow]) -> PortfolioStrategy:
    """The SAME 12-1 recipe as signals.xsmom.v1 (LOOKBACK/SKIP imported,
    identical deterministic tie-break), ranked over the point-in-time eligible
    set, holding the winner decile equal-weight."""
    def strat(view: PanelView) -> dict[str, float]:
        t = view.t
        ranked: list[tuple[float, str]] = []
        for s in pit_eligible(view, members):
            c_form = view.close(s, t - LOOKBACK)
            c_skip = view.close(s, t - SKIP)
            # contiguity: both exist for any eligible symbol (SEASONING == LOOKBACK)
            assert c_form is not None and c_skip is not None
            ranked.append((c_skip / c_form - 1.0, s))
        ranked.sort(key=lambda rs: (-rs[0], rs[1]))
        top = ranked[:winner_count(len(ranked))]
        if not top:
            return {}
        w = 1.0 / len(top)
        return {s: w for _, s in top}
    return strat


def pit_equal_weight(members: Mapping[str, MembershipRow]) -> PortfolioStrategy:
    """Informational benchmark: equal weight over ALL point-in-time eligible
    names, monthly (NOT binding — shown per protocol)."""
    def strat(view: PanelView) -> dict[str, float]:
        elig = pit_eligible(view, members)
        if not elig:
            return {}
        w = 1.0 / len(elig)
        return {s: w for s in elig}
    return strat


# ---------------------------------------------------------------------------
# Delisting-aware engine: portfolio.py's accounting move-for-move (equivalence
# pinned by test), plus the documented delisting rule. The frozen engine is
# deliberately untouched — it refuses mid-hold missing prices by design.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForcedLiquidation:
    day: date          # first session WITHOUT a bar; cash from this mark on
    symbol: str
    weight: float      # drifted weight liquidated (fraction of equity)


@dataclass(frozen=True)
class PitBacktest:
    result: PortfolioResult
    forced_liquidations: tuple[ForcedLiquidation, ...]
    unfilled_buys: tuple[tuple[date, str], ...]  # died between decision and execution


def _liquidate_dead(equity: float, weights: dict[str, float], panel: PricePanel,
                    i: int, side_rate: float,
                    log: list[ForcedLiquidation]) -> tuple[float, dict[str, float]]:
    """DELISTING RULE: a held name with no bar at session i was liquidated at
    its final available close — which IS the close[i-1] mark the equity curve
    already carries, so the conversion itself is value-neutral; the sale pays
    the same per-side cost as any other trade (conservative: a forced exit is
    never cheaper than a chosen one), and the surviving weights renormalise
    against the post-cost equity. Proceeds sit in cash until the next
    rebalance. (Open/close availability agree per the panel invariant, so one
    close-based check covers execution mornings too.)"""
    dead = [s for s in weights if panel.closes[s][i] is None]
    if not dead:
        return equity, weights
    w_dead = sum(weights[s] for s in dead)
    factor = 1.0 - w_dead * side_rate
    for s in dead:
        log.append(ForcedLiquidation(day=panel.dates[i], symbol=s,
                                     weight=weights[s]))
    return equity * factor, {s: w / factor for s, w in weights.items()
                             if s not in dead}


def run_pit_backtest(prices: PricePanel, strategy: PortfolioStrategy,
                     costs: CostModel = CostModel(), *,
                     start: date, end: date | None = None) -> PitBacktest:
    """Monthly rebalance at t (strategy sees only data <= t via PanelView),
    execution at the next session's open, per-side bps on turnover, daily
    close marks — identical to portfolio.run_portfolio_backtest, with the two
    delisting behaviours documented in the module docstring."""
    dates = prices.dates
    start_i = prices.index_at(start)
    end_i = len(dates) if end is None else bisect_right(dates, end)
    if end_i - start_i < 2:
        raise ValueError("window too short: need at least two sessions")
    side_rate = (costs.commission_bps + costs.slippage_bps) / 10_000
    reb = set(month_end_indices(dates, start_i, end_i))

    equity = 1.0
    curve = [equity]
    weights: dict[str, float] = {}
    pending: dict[str, float] | None = None
    turnovers: list[float] = []
    forced: list[ForcedLiquidation] = []
    unfilled: list[tuple[date, str]] = []

    if start_i in reb:
        pending = _validated_targets(strategy(PanelView(prices, start_i)),
                                     prices, start_i)
    for i in range(start_i + 1, end_i):
        equity, weights = _liquidate_dead(equity, weights, prices, i,
                                          side_rate, forced)
        if pending is not None:
            dead_targets = sorted(s for s in pending
                                  if prices.closes[s][i] is None)
            for s in dead_targets:
                unfilled.append((dates[i], s))
                del pending[s]
            # execute at today's open: drift to open, trade, pay per-side costs
            equity, weights = _drift(equity, weights, prices, i, phase="to_open")
            t_over = turnover(weights, pending)
            equity *= 1.0 - t_over * side_rate
            weights = dict(pending)
            turnovers.append(t_over)
            pending = None
            equity, weights = _drift(equity, weights, prices, i, phase="open_close")
        else:
            equity, weights = _drift(equity, weights, prices, i, phase="close")
        curve.append(equity)
        if i in reb:
            pending = _validated_targets(strategy(PanelView(prices, i)), prices, i)

    rets = [curve[j] / curve[j - 1] - 1 for j in range(1, len(curve))]
    mu = statistics.fmean(rets) if rets else 0.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mu / sd) * (252 ** 0.5) if sd > 0 else 0.0
    peak, mdd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    result = PortfolioResult(
        total_return=curve[-1] - 1.0, sharpe=sharpe, max_drawdown=mdd,
        avg_turnover=statistics.fmean(turnovers) if turnovers else 0.0,
        n_rebalances=len(turnovers), equity_curve=curve,
        dates=list(dates[start_i:end_i]))
    return PitBacktest(result=result, forced_liquidations=tuple(forced),
                       unfilled_buys=tuple(unfilled))


def pit_null_distribution(panel: PricePanel,
                          members: Mapping[str, MembershipRow], *,
                          costs: CostModel, start: date, paths: int,
                          seed: int) -> list[float]:
    """Seeded monkey portfolios (ADR-0002 #2): at each rebalance, the SAME
    COUNT of names the strategy would hold (winner_count of the identical
    eligible set) drawn uniformly without replacement, equal weight, through
    the IDENTICAL delisting-aware engine. One rng drives all paths
    sequentially (the validation.py convention); eligible sets are cached per
    rebalance index — a pure property of panel + membership."""
    rng = random.Random(seed)
    cache: dict[int, list[str]] = {}

    def monkey(view: PanelView) -> dict[str, float]:
        elig = cache.get(view.t)
        if elig is None:
            elig = pit_eligible(view, members)
            cache[view.t] = elig
        if not elig:
            return {}
        pick = rng.sample(elig, min(winner_count(len(elig)), len(elig)))
        w = 1.0 / len(pick)
        return {s: w for s in pick}

    return [run_pit_backtest(panel, monkey, costs, start=start).result.total_return
            for _ in range(paths)]


def pit_walk_forward(panel: PricePanel, strategy: PortfolioStrategy, *,
                     k: int, horizon: int, embargo: int, warmup: int,
                     costs: CostModel) -> PortfolioWalkForwardResult:
    """Purged + embargoed folds on the daily session timeline (constants from
    real_run, leakage_free re-asserted per fold — the ETF-run convention),
    driven through the delisting-aware engine. warmup is the evaluation-window
    start index: it dominates SEASONING and keeps every fold's test window
    inside the membership-reliable window (>= WINDOW_START)."""
    results: list[PortfolioResult] = []
    for fold in purged_folds(len(panel.dates), k=k, horizon=horizon,
                             embargo=embargo, warmup=warmup):
        assert leakage_free(fold, horizon=horizon, embargo=embargo)
        results.append(run_pit_backtest(
            panel, strategy, costs,
            start=panel.dates[fold.test_start],
            end=panel.dates[fold.test_end - 1]).result)
    rets = [r.total_return for r in results]
    return PortfolioWalkForwardResult(
        fold_results=results,
        mean_return=statistics.fmean(rets),
        mean_sharpe=statistics.fmean(r.sharpe for r in results),
        worst_fold_return=min(rets),
        positive_folds=sum(1 for x in rets if x > 0))


# ---------------------------------------------------------------------------
# Panel loading: unlike xsmom_run.load_universe_panel, series that END EARLY
# are KEPT — dead companies are the point of this test. Everything else
# (per-instrument completeness, off-calendar refusal, non-US exclusion) is the
# same documented fail-closed rule set.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PitExclusion:
    symbol: str
    delisted: bool
    reason: str


@dataclass(frozen=True)
class PitUniverse:
    panel: PricePanel
    members: dict[str, MembershipRow]     # panel symbols that carry membership
    partition: MembershipPartition        # full-table usability split (report)
    window_rows: tuple[MembershipRow, ...]  # usable members intersecting window
    included_living: int
    included_delisted: int
    missing_series: list[PitExclusion]    # member tickers with NO stored bars
    excluded: list[PitExclusion]          # stored but failed completeness rules

    @property
    def window_members(self) -> int:
        return len(self.window_rows)


def load_pit_panel(session: Session, *, window_end: date | None = None,
                   index_code: str = INDEX_CODE) -> PitUniverse:
    """Aligned open/close matrix over every window-relevant member ticker with
    a usable stored series, PLUS the SPY benchmark (no membership row, so it
    can never be ranked — asserted). Coverage is reported honestly: a member
    whose series is missing or unusable is counted, split living/delisted.
    window_end defaults to the last stored vendor bar (the data decides)."""
    if window_end is None:
        window_end = session.execute(text(
            "SELECT max(bar_date) FROM market.price_bars_daily "
            "WHERE source = 'EodhdAdapter'")).scalar()
        if window_end is None:
            raise RuntimeError("no vendor bars stored — run the backfill first")
    all_rows = load_membership(session, index_code=index_code)
    if not all_rows:
        raise RuntimeError(f"no membership rows for {index_code} — run "
                           "`python -m atlas.dcp.market_data.index_membership "
                           "fetch` first")
    part = partition_membership(all_rows)
    window_rows = tuple(r for r in part.usable
                        if member_in_window(r, WINDOW_START, window_end))
    members = {r.ticker: r for r in window_rows}
    if BENCHMARK in members:
        raise RuntimeError(f"benchmark {BENCHMARK} appears in the membership "
                           "table — it must stay outside the ranked universe")

    wanted = sorted(members) + [BENCHMARK]
    rows = session.execute(text(
        "SELECT DISTINCT i.symbol, i.market FROM market.instruments i "
        "JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
        "WHERE pb.source = 'EodhdAdapter' AND i.symbol = ANY(:syms) "
        "ORDER BY i.symbol"), {"syms": wanted}).all()
    stored = {r.symbol: r.market for r in rows}
    if BENCHMARK not in stored:
        raise RuntimeError(f"benchmark {BENCHMARK} has no vendor bars — run "
                           "the backfill first")

    def _delisted(sym: str) -> bool:
        row = members.get(sym)
        return row.is_delisted if row is not None else False

    missing = [PitExclusion(sym, _delisted(sym), "no stored vendor bars "
                            "(backfill failed or vendor never served it)")
               for sym in sorted(members) if sym not in stored]
    excluded: list[PitExclusion] = []
    series: dict[str, tuple[list[float], list[float], list[date]]] = {}
    for sym in sorted(stored):
        if stored[sym] != "US":
            excluded.append(PitExclusion(sym, _delisted(sym),
                                         f"non-US session calendar "
                                         f"(market={stored[sym]})"))
            continue
        obars, ds = load_adjusted_obars(session, sym)
        expected = trading_days_between("US", ds[0], ds[-1])
        have = set(ds)
        gaps = [d for d in expected if d not in have]
        if gaps:
            excluded.append(PitExclusion(
                sym, _delisted(sym),
                f"{len(gaps)} missing session(s) between its inception {ds[0]} "
                f"and end {ds[-1]} (first: {gaps[0]})"))
            continue
        off_cal = sorted(have - set(expected))
        if off_cal:
            excluded.append(PitExclusion(
                sym, _delisted(sym),
                f"{len(off_cal)} bar(s) on non-session dates (first: {off_cal[0]})"))
            continue
        series[sym] = ([b.open for b in obars], [b.close for b in obars], ds)
    if BENCHMARK not in series:
        raise RuntimeError(f"benchmark {BENCHMARK} failed the completeness "
                           "rules — fix its series before evaluating")

    first = min(ds[0] for _, _, ds in series.values())
    last = max(ds[-1] for _, _, ds in series.values())
    dates = trading_days_between("US", first, last)
    idx = {d: i for i, d in enumerate(dates)}
    opens: dict[str, list[float | None]] = {}
    closes: dict[str, list[float | None]] = {}
    for sym, (o, c, ds) in series.items():
        oo: list[float | None] = [None] * len(dates)
        cc: list[float | None] = [None] * len(dates)
        for j, d in enumerate(ds):
            oo[idx[d]] = o[j]
            cc[idx[d]] = c[j]
        opens[sym], closes[sym] = oo, cc

    in_panel = [s for s in series if s in members]
    return PitUniverse(
        panel=PricePanel(dates=dates, opens=opens, closes=closes),
        members={s: members[s] for s in in_panel},
        partition=part, window_rows=window_rows,
        included_living=sum(1 for s in in_panel if not members[s].is_delisted),
        included_delisted=sum(1 for s in in_panel if members[s].is_delisted),
        missing_series=missing, excluded=excluded)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class XsmomPitRun:
    universe: PitUniverse
    start: date
    run: PitBacktest
    spy: PortfolioResult
    ew: PortfolioResult
    gate: PortfolioGateReport
    wf: PortfolioWalkForwardResult
    trial_id: str
    n_trials: int
    trials_before_total: int
    trials_after_total: int
    member_counts: list[tuple[date, int, int]]  # (rebalance, members, eligible)


def run_xsmom_pit(session: Session, audit: PostgresAuditLog, *,
                  paths: int = 1000, seed: int = 7) -> XsmomPitRun:
    universe = load_pit_panel(session)
    panel = universe.panel
    members = universe.members
    start_i = bisect_left(panel.dates, WINDOW_START)
    if start_i >= len(panel.dates):
        raise RuntimeError(f"panel ends before WINDOW_START {WINDOW_START}")
    if start_i < SEASONING:
        raise RuntimeError(
            f"only {start_i} sessions precede {WINDOW_START} — the first "
            f"rebalance needs {SEASONING} sessions of formation history "
            "(backfill from PRICE_START first)")
    start = panel.dates[start_i]
    strategy = xsmom_pit_strategy(members)

    pit = run_pit_backtest(panel, strategy, COSTS, start=start)
    result = pit.result

    # per-rebalance counts (report data-quality section): reconstructed
    # membership from the FULL usable window rows (series or not) vs the
    # eligible set the strategy actually ranked
    member_counts: list[tuple[date, int, int]] = []
    for t in month_end_indices(panel.dates, start_i, len(panel.dates)):
        day = panel.dates[t]
        n_members = sum(1 for r in universe.window_rows if is_member_on(r, day))
        n_elig = len(pit_eligible(PanelView(panel, t), members))
        member_counts.append((day, n_members, n_elig))

    trials_before_total = total_trial_count(session)
    spec: dict[str, object] = {
        **SPEC, "family": FAMILY,
        "universe": f"point-in-time {INDEX_CODE} membership "
                    "(validation.index_membership, fail-closed interval rule)",
        "window_start": str(WINDOW_START),
        "window": f"{panel.dates[0]}..{panel.dates[-1]}", "start": str(start),
        "top_n": f"winner decile: max({TOP_N}, n_eligible // {DECILE})",
        "membership_rows": len(universe.partition.usable)
        + len(universe.partition.excluded_null_start_delisted)
        + len(universe.partition.excluded_null_start_departed),
        "membership_excluded_null_start_delisted":
            len(universe.partition.excluded_null_start_delisted),
        "membership_excluded_null_start_departed":
            len(universe.partition.excluded_null_start_departed),
        "members_in_window": universe.window_members,
        "members_with_series": len(members),
        "members_missing_series": len(universe.missing_series),
        "delisting_rule": "liquidate at final available close, per-side cost, "
                          "proceeds in cash until next rebalance",
        "data": "EODHD real",
        "costs_bps_per_side": COSTS.commission_bps + COSTS.slippage_bps}
    trial_id = register_trial(
        session, family=FAMILY, spec=spec,
        metrics={"total_return": result.total_return, "sharpe": result.sharpe,
                 "max_drawdown": result.max_drawdown,
                 "avg_turnover": result.avg_turnover,
                 "n_rebalances": float(result.n_rebalances)})
    n_trials = trial_count(session, FAMILY)
    trials_after_total = total_trial_count(session)

    nulls = pit_null_distribution(panel, members, costs=COSTS, start=start,
                                  paths=paths, seed=seed)
    spy = run_pit_backtest(panel, buy_and_hold_strategy(BENCHMARK), COSTS,
                           start=start).result
    ew = run_pit_backtest(panel, pit_equal_weight(members), COSTS,
                          start=start).result
    gate = portfolio_gate(result=result, null_returns=nulls, spy=spy, ew=ew,
                          n_trials=n_trials)
    wf = pit_walk_forward(panel, strategy, k=K_FOLDS, horizon=HORIZON,
                          embargo=EMBARGO, warmup=start_i, costs=COSTS)

    audit.append(
        event_type="quant.backtest.completed", entity_type="strategy",
        entity_id=f"{FAMILY}/portfolio", actor_type="dcp",
        actor_id="xsmom_pit_run",
        payload={"universe": f"point-in-time {INDEX_CODE}",
                 "members_in_window": universe.window_members,
                 "members_with_series": len(members),
                 "members_missing_series": len(universe.missing_series),
                 "included_delisted": universe.included_delisted,
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
                 "survivorship_note": "point-in-time membership INCLUDING "
                                      "delisted names — the definitive test "
                                      "of the S&P-100/ETF conditional chain"})
    return XsmomPitRun(universe=universe, start=start, run=pit, spy=spy, ew=ew,
                       gate=gate, wf=wf, trial_id=trial_id, n_trials=n_trials,
                       trials_before_total=trials_before_total,
                       trials_after_total=trials_after_total,
                       member_counts=member_counts)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _annual_distribution_lines(run: XsmomPitRun) -> list[str]:
    """House rule: an earnings profile is derived ONLY for a validated pass —
    profit is a result to be discovered, never an input."""
    lines = ["## Annual outcome distribution", ""]
    if not run.gate.passed:
        lines += [
            "No distribution is derived for a failed strategy (house rule: "
            "earnings profiles are derived only for validated strategies — "
            "profit is a result to be discovered, never an input).",
            "",
        ]
        return lines
    lines += [
        "> **History is not a forecast.** This is the DISPERSION a strategy "
        "like this has",
        "> exhibited — any single future year can land anywhere in (or "
        "outside) this range;",
        "> the median is not a promise.",
        "",
        "Per-calendar-year returns (identical engine, window and costs for "
        "both columns; partial years noted):",
        "",
        "| year | strategy | SPY B&H | note |",
        "|---|---|---|---|",
    ]
    strat_years = calendar_year_returns(run.run.result)
    spy_years = {y.year: y for y in calendar_year_returns(run.spy)}
    if set(spy_years) != {y.year for y in strat_years}:
        raise RuntimeError("strategy and SPY cover different years — the "
                           "shared-window invariant is broken")
    for y in strat_years:
        lines.append(f"| {y.year} | {y.ret:+.2%} | {spy_years[y.year].ret:+.2%} "
                     f"| {y.note} |")
    strat_draws = block_bootstrap_annual(daily_returns(run.run.result))
    spy_draws = block_bootstrap_annual(daily_returns(run.spy))
    lines += [
        "",
        f"Block bootstrap of annual outcomes: daily returns resampled in "
        f"{BOOT_BLOCK}-session blocks, {BOOT_DRAWS} seeded draws of "
        f"{BOOT_HORIZON} sessions (seed {BOOT_SEED}). The rng stream depends "
        "only on (seed, series length), so strategy and SPY draw identical "
        "block positions — paired draws, same method for both columns.",
        "",
        "| percentile of simulated annual return | strategy | SPY B&H |",
        "|---|---|---|",
        *[f"| {label} | {percentile(strat_draws, q):+.2%} "
          f"| {percentile(spy_draws, q):+.2%} |" for label, q in _PCTS],
        "",
    ]
    return lines


def render_pit_report(run: XsmomPitRun, *, paths: int) -> str:
    panel, g, wf, r = run.universe.panel, run.gate, run.wf, run.run.result
    part = run.universe.partition
    verdict = "PASS" if g.passed else "FAIL"
    implication = (
        "cross-sectional momentum on the point-in-time S&P 500 — dead "
        "companies included — clears the full gauntlet: the effect is real "
        "AND beats the fund's actual alternative; the S&P-100 (+4,584%) "
        "magnitude stays inflated by survivorship, but the strategy family "
        "is validated on honest membership"
        if g.passed else
        "with survivorship truly removed (point-in-time membership, dead "
        "companies included), the recipe does not clear the fund's bar; the "
        "conditional S&P-100 PASS is settled as a survivorship artifact, "
        "consistent with the sector-ETF cross-check FAIL, and the xsmom "
        "family must not proceed toward approval")
    fold_rets = ", ".join(f"{x.total_return:+.2%}" for x in wf.fold_results)
    decision_grade = (panel.dates[-1] - run.start).days >= 3650
    n_members = len(run.universe.members)
    n_missing = len(run.universe.missing_series)
    n_excluded = len(run.universe.excluded)
    miss_delisted = [e for e in run.universe.missing_series if e.delisted]
    excl_delisted = [e for e in run.universe.excluded if e.delisted]
    total_delisted_members = (run.universe.included_delisted
                              + len(miss_delisted) + len(excl_delisted))
    delisted_cov = (run.universe.included_delisted / total_delisted_members
                    if total_delisted_members else 1.0)
    first_counts = run.member_counts[0]
    last_counts = run.member_counts[-1]
    yearly = [mc for mc in run.member_counts if mc[0].month == 12]

    lines = [
        "# THE DEFINITIVE MOMENTUM TEST — xsmom recipe on the point-in-time "
        "S&P 500, dead companies included (2026-07)",
        "",
        "> ## WHY THIS IS THE DEFINITIVE TEST",
        "> Membership is POINT-IN-TIME: at every rebalance the ranked "
        "universe is the",
        "> S&P 500 as it stood THAT DAY (validation.index_membership, "
        "vendor's",
        "> HistoricalTickerComponents), INCLUDING companies that later "
        "collapsed, were",
        "> acquired, or were delisted — their price series are in the panel "
        "and a held",
        "> name that dies mid-hold is liquidated at its final available "
        "close. This",
        "> removes the index-membership survivorship bias that made the "
        "S&P-100 result",
        "> conditional (docs/reports/xsmom-momentum-2026-07.md) and that the "
        "sector-ETF",
        "> cross-check (docs/reports/xsmom-etf-crosscheck-2026-07.md) could "
        "only probe",
        "> on nine fixed funds. It settles the survivorship question for "
        "this recipe.",
        "",
        *(["> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)",
           f"> Evaluation window {run.start} → {panel.dates[-1]} (>= 10 "
           "years); the verdict is",
           "> decision-grade FOR THE SURVIVORSHIP QUESTION — pass or fail, "
           "recorded verbatim.",
           ""] if decision_grade else
          ["> ## ⚠️ SMALL-SAMPLE WARNING (ADR-0004)",
           "> Short window; verdicts are **not decision-grade**.",
           ""]),
        "Same textbook recipe (Jegadeesh & Titman 1993, 12-1, monthly, equal "
        "weight,",
        f"{SEASONING}-session seasoning), zero parameter sweeps. Winner "
        "portfolio is the",
        f"TOP DECILE of the point-in-time eligible set (n_eligible // "
        f"{DECILE}, floored at",
        f"v1's {TOP_N}) — the J&T construction is fractional, and the "
        "eligible set now",
        "varies month to month. ONE registered trial (family "
        f"`{FAMILY}`). Gate",
        "thresholds are IMPORTED from the committed validation module — "
        "nothing restated,",
        "nothing tuned.",
        "",
        f"- Evaluation window STARTS {WINDOW_START}: vendor EndDates are "
        "sparse before ~2012",
        "  (prong-B probe), so earlier membership is unreliable — documented "
        "fail-closed bound",
        f"- Engine: portfolio target-weight, monthly rebalance at month-end "
        f"close, execution at next session's open, costs "
        f"{COSTS.commission_bps}+{COSTS.slippage_bps} bps/side on turnover",
        "- DELISTING RULE (hand-pinned by test): a held name whose series "
        "ends mid-hold is",
        "  liquidated at its final available close, pays the same per-side "
        "cost, and the",
        "  proceeds sit in cash until the next rebalance; a pending buy "
        "whose name dies",
        "  between decision and execution does not fill",
        f"- Null model: {paths}-path monkey MC — at each rebalance, the SAME "
        "COUNT of names",
        "  drawn uniformly from the SAME point-in-time eligible set, "
        "identical engine/costs/",
        "  delisting rule (ADR-0002 #2)",
        f"- Walk-forward: purged+embargoed on the daily timeline, k={K_FOLDS}, "
        f"horizon={HORIZON}, embargo={EMBARGO} (constants from real_run), "
        "warmup = the evaluation-window start index (dominates "
        f"{SEASONING}-session seasoning and keeps every fold past "
        f"{WINDOW_START}) (ADR-0002 #3)",
        "- Registered in quant.trial_registry; deflated Sharpe uses the true "
        "family trial count (ADR-0002 #1)",
        "- Benchmark: SPY buy-and-hold over the same window — the BINDING "
        "comparison per ADR-0009; SPY carries no membership row and can "
        "never be ranked; equal-weight-all-eligible shown per protocol, NOT "
        "binding",
        "- Convention note (inherited from the round-2 machinery, applied "
        "identically to strategy, null and both benchmarks): bars are "
        "split-adjusted PRICE returns — dividends are not reinvested on "
        "either side of the comparison",
        "",
        "## Data quality and honesty",
        "",
        "### Membership reconstruction (fail-closed rule, "
        "market_data/index_membership.py)",
        "",
        f"- Vendor rows: {len(part.usable) + len(part.excluded_null_start_delisted) + len(part.excluded_null_start_departed)} "
        f"total; usable {len(part.usable)}; EXCLUDED fail-closed: "
        f"{len(part.excluded_null_start_delisted)} null-StartDate+delisted, "
        f"{len(part.excluded_null_start_departed)} null-StartDate+departed "
        "(unknowable intervals; several demonstrably carry ticker-reuse "
        "confusion)",
        f"- Usable members intersecting the window: {run.universe.window_members}",
        f"- ⚠️ RECONSTRUCTION UNDERCOUNT: at the first rebalance "
        f"({first_counts[0]}) the reconstructed membership is "
        f"{first_counts[1]} names (true S&P 500 ≈ 500) because every "
        "null-StartDate row was excluded fail-closed — and those missing "
        "rows are ALL departures (names that later left the index). The "
        "early-window eligible set is therefore still survivor-tilted at "
        "the margin; this bias is one-directional (it FLATTERS momentum, "
        "as the S&P-100 run showed) and shrinks to zero by "
        f"{last_counts[0]} ({last_counts[1]} members).",
        "- Members/eligible at each December rebalance: "
        + "; ".join(f"{d.year}: {m}/{e}" for d, m, e in yearly),
        "",
        "### Price coverage (per-instrument completeness, fail closed per "
        "series)",
        "",
        f"- {n_members} of {run.universe.window_members} window members have "
        f"usable series in the panel ({run.universe.included_living} living, "
        f"{run.universe.included_delisted} delisted)",
        f"- Missing series (no stored vendor bars): {n_missing} "
        f"({len(miss_delisted)} delisted)",
        f"- Excluded by completeness rules: {n_excluded} "
        f"({len(excl_delisted)} delisted)",
        f"- DELISTED-member price coverage: {run.universe.included_delisted} "
        f"of {total_delisted_members} = {delisted_cov:.0%}"
        + ("" if delisted_cov >= 0.6 else
           " — ⚠️ BELOW 60%: missing dead names re-introduce the very bias "
           "under test; treat any PASS as unproven"),
        f"- Forced delisting liquidations during the run: "
        f"{len(run.run.forced_liquidations)}; unfilled buys (died between "
        f"decision and execution): {len(run.run.unfilled_buys)}",
        "",
    ]
    if run.universe.excluded:
        lines += ["Excluded series (first 30):",
                  *[f"  - {e.symbol}{' [delisted]' if e.delisted else ''}: "
                    f"{e.reason}" for e in run.universe.excluded[:30]], ""]
    if run.universe.missing_series:
        lines += [f"Missing series ({len(run.universe.missing_series)}): "
                  + ", ".join(f"{e.symbol}{'*' if e.delisted else ''}"
                              for e in run.universe.missing_series)
                  + "  (* = delisted)", ""]
    lines += [
        f"## Full-window result (start {run.start}, panel "
        f"{panel.dates[0]} → {panel.dates[-1]}, "
        f"{len(panel.dates)} aligned XNYS sessions, split-adjusted)",
        "",
        f"Return {r.total_return:+.2%}, Sharpe {r.sharpe:.2f}, max drawdown "
        f"{r.max_drawdown:.2%}, avg turnover {r.avg_turnover:.2%} per "
        f"rebalance (sum |Δw|, both sides), {r.n_rebalances} rebalances",
        "",
        f"### Gate verdict: **{verdict}**",
        "",
        f"- verdict: **{verdict}**",
        f"- implication for the S&P-100 → ETF results chain: {implication}",
        f"- strategy return: {g.strategy_return:+.2%}",
        f"- SPY buy-and-hold (BINDING benchmark per ADR-0009 — the fund's "
        f"actual alternative): {g.spy_bh_return:+.2%}",
        f"- equal-weight all-eligible, monthly (informational, shown per "
        f"protocol, NOT binding): {g.ew_return:+.2%}",
        f"- null-model p-value: {g.null_p_value:.3f} (must be ≤ {P_MAX})",
        f"- deflated Sharpe: {g.dsr:.3f} at n_trials={g.n_trials} "
        f"(must be ≥ {DSR_MIN})",
        f"- trial registry id: `{run.trial_id}`",
        "",
    ]
    if g.reasons:
        lines.append("Verbatim gate reasons:")
        lines += [f"- {reason}" for reason in g.reasons]
        lines.append("")
    lines += [
        f"### Walk-forward: {wf.positive_folds}/{len(wf.fold_results)} "
        "folds positive",
        "",
        f"- fold returns: {fold_rets}",
        f"- mean return {wf.mean_return:+.2%}, mean Sharpe "
        f"{wf.mean_sharpe:.2f}, worst fold {wf.worst_fold_return:+.2%}",
        "",
        "## Summary",
        "",
        "| strategy | return | SPY B&H | EW eligible | Sharpe | max DD "
        "| avg turnover | rebalances | null p | DSR (n_trials) | WF folds + "
        "| verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        f"| xsmom recipe, PIT S&P 500 winner decile | {r.total_return:+.2%} "
        f"| {g.spy_bh_return:+.2%} "
        f"| {g.ew_return:+.2%} | {r.sharpe:.2f} | {r.max_drawdown:.2%} "
        f"| {r.avg_turnover:.2%} | {r.n_rebalances} | {g.null_p_value:.3f} "
        f"| {g.dsr:.3f} ({g.n_trials}) "
        f"| {wf.positive_folds}/{len(wf.fold_results)} | **{verdict}** |",
        "",
        f"Implication: {implication}.",
        "",
        f"Trial registry: **{run.trials_before_total} trials before this run "
        f"→ {run.trials_after_total} after** (ONE {FAMILY} trial; family "
        f"count now {run.n_trials}).",
        "",
        *_annual_distribution_lines(run),
        "## Approval status",
        "",
        "**None sought here — by design.** This is a VALIDATION run on a "
        "membership-gated universe built from validation-only instruments "
        "(is_active=FALSE): it settles the survivorship question for the "
        "xsmom family; it does not itself qualify any strategy for the "
        "approval workflow (dcp/backtest/approval.py). The gates were not "
        "modified; no strategy row is touched.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(
        description="Definitive point-in-time S&P 500 xsmom evaluation")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--report", type=Path,
                   default=ROOT / "docs" / "reports" / "xsmom-pit-sp500-2026-07.md")
    a = p.parse_args()

    with session_scope() as s:
        # deterministic clock: derived from the data, not the wall
        last_bar = s.execute(text(
            "SELECT max(bar_date) FROM market.price_bars_daily "
            "WHERE source='EodhdAdapter'")).scalar()
        if last_bar is None:
            raise SystemExit("no real bars in the database — run the backfill first")
        clock = FrozenClock(datetime(last_bar.year, last_bar.month, last_bar.day,
                                     22, 0, tzinfo=UTC))
        audit = PostgresAuditLog(s, clock)
        run = run_xsmom_pit(s, audit, paths=a.paths)

    report = render_pit_report(run, paths=a.paths)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(report)
    g = run.gate
    print(f"{FAMILY}/portfolio: gate={'PASS' if g.passed else 'FAIL'} "
          f"return={g.strategy_return:+.2%} spy={g.spy_bh_return:+.2%} "
          f"ew={g.ew_return:+.2%} p={g.null_p_value:.3f} dsr={g.dsr:.3f} "
          f"wf={run.wf.positive_folds}/{len(run.wf.fold_results)} "
          f"(reasons: {list(g.reasons) or 'none'})")
    print(f"members: {len(run.universe.members)} with series "
          f"({run.universe.included_delisted} delisted); "
          f"missing {len(run.universe.missing_series)}; "
          f"forced liquidations {len(run.run.forced_liquidations)}")
    print(f"trials: {run.trials_before_total} -> {run.trials_after_total} "
          f"({FAMILY} family: {run.n_trials})")
    print(f"report written: {a.report}")


if __name__ == "__main__":
    main()
