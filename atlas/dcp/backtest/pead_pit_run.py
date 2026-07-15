"""THE EARNINGS-SURPRISE TEST: PEAD / SUE on the POINT-IN-TIME S&P 500 — the
one orthogonal factor added through the IDENTICAL bar the momentum recipe
passed. The ONLY difference versus xsmom-pit is the signal: names are ranked by
Standardized Unexpected Earnings (Foster-Olsen-Shevlin SUE;
signals.pead.v1) instead of 12-1 price momentum. Everything else — the
point-in-time membership universe (INCLUDING delisted names), the
delisting-aware engine, the top-decile equal-weight monthly construction, the
1000-path monkey null, the deflated Sharpe at the TRUE registered trial count,
the purged+embargoed walk-forward, the binding beat-SPY-total-return bar, and
the 10 bps/side costs — is REUSED BY IMPORT from xsmom_pit_run and the committed
portfolio gauntlet, never restated.

WHY THIS FACTOR. An external quant review argued Atlas has a single alpha
source (momentum). Earnings-estimate *revisions* proper are not
point-in-time-backtestable on our vendor (EODHD overwrites the estimate-trend
history), so this is the backtestable cousin with clean deep data: earnings
SURPRISE and its post-announcement drift.

NO LOOK-AHEAD is STRUCTURAL and lives in the signal (signals/pead/v1.py): the
report_date gates when a surprise becomes knowable (after-market prints land
the next session), and a report dated after the decision session physically
cannot enter the ranking. See that module's header.

EARNINGS PANEL. Alongside the price panel (load_pit_panel, reused verbatim —
dead series kept, SPY outside the ranked universe) the runner builds an
EarningsView over the SAME session calendar from market.earnings_surprises,
split-adjusting EPS on read via the house convention. The panel is IDENTICAL in
price and total-return modes (surprises are EPS facts, untouched by dividend
reinvestment).

TOTAL-RETURN MODE (--total-return): ADR-0009's binding benchmark is SPY TOTAL
RETURN. The panel is transformed at load time (dividends reinvested at the
ex-date close, applied identically to strategy, monkey null, EW and SPY), TWO
trials run — family 'pead-sue-tr' (identical window) and the pre-committed
KILL-ONLY 'pead-sue-tr-<year>' (later start; can only demote) — and the
verdict-vs-endpoint exhibit rolls the final date back monthly.

Do NOT tune anything to pass. A failed gate is a valid, reportable result; the
graveyard verdict recorded verbatim IS the deliverable.

Usage: python -m atlas.dcp.backtest.pead_pit_run [--paths 1000]
       python -m atlas.dcp.backtest.pead_pit_run --total-return [--paths 1000]
"""
from __future__ import annotations

import argparse
import random
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
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
from atlas.dcp.backtest.real_run import COSTS, EMBARGO, HORIZON, K_FOLDS
from atlas.dcp.backtest.registry import register_trial, trial_count
from atlas.dcp.backtest.validation import deflated_sharpe
from atlas.dcp.backtest.xsmom_pit_run import (
    BENCHMARK,
    DECILE,
    ENDPOINT_MONTHS,
    EndpointVerdict,
    PitBacktest,
    PitUniverse,
    _return_at,
    _sharpe_at,
    load_pit_panel,
    pit_walk_forward,
    run_pit_backtest,
    winner_count,
)
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
from atlas.dcp.market_data.earnings_history import EarningsSurprise
from atlas.dcp.market_data.index_membership import (
    INDEX_CODE,
    WINDOW_START,
    MembershipRow,
    is_member_on,
)
from atlas.dcp.market_data.models import Split
from atlas.dcp.signals.pead.v1 import (
    SPEC,
    STALENESS_SESSIONS,
    STANDARDIZE_MIN,
    STANDARDIZE_WINDOW,
    EarningsView,
    build_earnings_view,
    pead_eligible,
)

ROOT = Path(__file__).resolve().parents[3]
FAMILY = "pead-sue"
FAMILY_TR = "pead-sue-tr"
VARIANT = "sue"                        # primary signal; surprise_pct is the cross-check
KILL_START = date(2016, 1, 1)         # pre-committed kill-only TR start (demote only)
PRICE_CONVENTION = "price (split-adjusted; dividends not reinvested)"
TR_CONVENTION = ("total return (split-adjusted; each dividend reinvested at its "
                 "ex-date close — market_data/total_return.py)")
PRICE_REPORT = ROOT / "docs" / "reports" / "pead-sue-pit-sp500.md"
TR_REPORT = ROOT / "docs" / "reports" / "pead-sue-pit-total-return.md"


def tr_family(window_start: date | None) -> str:
    return FAMILY_TR if window_start is None else f"{FAMILY_TR}-{window_start.year}"


# ---------------------------------------------------------------------------
# Earnings panel loading (from market.earnings_surprises + house split reads)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PeadCoverage:
    symbols_in_panel: int
    symbols_with_reports: int      # panel members carrying any stored surprise
    total_reports: int
    symbols_with_signal: int       # symbols with >= 1 in-window standardizable SUE
    delisted_with_reports: int


def load_pead_signals(session: Session, symbols: list[str], dates: list[date],
                      members: Mapping[str, MembershipRow],
                      ) -> tuple[EarningsView, PeadCoverage]:
    """Build the point-in-time EarningsView over the panel's session calendar
    from market.earnings_surprises (RAW EPS) and market.corporate_actions
    splits (house adjust-on-read). Coverage is reported honestly."""
    reports: dict[str, list[EarningsSurprise]] = {}
    for r in session.execute(text(
            "SELECT i.symbol, es.fiscal_period_end, es.report_date, "
            "       es.eps_actual, es.eps_estimate, es.surprise_pct, "
            "       es.before_after_market "
            "FROM market.earnings_surprises es "
            "JOIN market.instruments i ON i.id = es.instrument_id "
            "WHERE i.symbol = ANY(:syms) "
            "ORDER BY i.symbol, es.fiscal_period_end"), {"syms": symbols}).all():
        reports.setdefault(r.symbol, []).append(EarningsSurprise(
            symbol=r.symbol, fiscal_period_end=r.fiscal_period_end,
            report_date=r.report_date, eps_actual=Decimal(r.eps_actual),
            eps_estimate=Decimal(r.eps_estimate),
            surprise_pct=(Decimal(r.surprise_pct)
                          if r.surprise_pct is not None else None),
            before_after_market=r.before_after_market, currency=None))
    splits: dict[str, list[Split]] = {}
    for r in session.execute(text(
            "SELECT i.symbol, ca.action_date, ca.ratio "
            "FROM market.corporate_actions ca "
            "JOIN market.instruments i ON i.id = ca.instrument_id "
            "WHERE ca.action_type = 'split' AND i.symbol = ANY(:syms)"),
            {"syms": symbols}).all():
        splits.setdefault(r.symbol, []).append(Split(
            symbol=r.symbol, action_date=r.action_date, ratio=Decimal(r.ratio)))
    view = build_earnings_view(reports, splits, dates)
    with_signal = sum(1 for s in view.symbols())
    delisted_with = sum(1 for s in reports
                        if members.get(s) is not None and members[s].is_delisted)
    cov = PeadCoverage(
        symbols_in_panel=len(symbols),
        symbols_with_reports=len(reports),
        total_reports=sum(len(v) for v in reports.values()),
        symbols_with_signal=with_signal,
        delisted_with_reports=delisted_with)
    return view, cov


# ---------------------------------------------------------------------------
# Point-in-time PEAD eligibility, strategy, benchmarks, null — the ONLY
# difference vs xsmom_pit is the signal; the engine is imported unchanged.
# ---------------------------------------------------------------------------

def pead_pit_eligible(view: PanelView, earnings: EarningsView,
                      members: Mapping[str, MembershipRow], *,
                      variant: str = VARIANT) -> list[str]:
    """Index member on the view's date (fail-closed interval rule) AND a live,
    fresh, defined surprise signal AND a price at t. Shared verbatim by
    strategy, monkey null and the equal-weight benchmark, so all face the
    identical universe by construction."""
    today = view.today
    out: list[str] = []
    for s in pead_eligible(view, earnings, variant=variant):
        row = members.get(s)
        if row is None or not is_member_on(row, today):
            continue
        out.append(s)
    return out


def pead_pit_strategy(members: Mapping[str, MembershipRow], earnings: EarningsView,
                      *, variant: str = VARIANT) -> PortfolioStrategy:
    """Rank the point-in-time eligible set by live signal DESCENDING, hold the
    winner decile equal-weight (winner_count imported from xsmom_pit for
    apples-to-apples), deterministic symbol tie-break."""
    def strat(view: PanelView) -> dict[str, float]:
        t = view.t
        ranked: list[tuple[float, str]] = []
        for s in pead_pit_eligible(view, earnings, members, variant=variant):
            sig = earnings.live(s, t, variant=variant)
            assert sig is not None  # pead_pit_eligible guarantees it
            ranked.append((sig, s))
        ranked.sort(key=lambda rs: (-rs[0], rs[1]))
        top = ranked[:winner_count(len(ranked))]
        if not top:
            return {}
        w = 1.0 / len(top)
        return {s: w for _, s in top}
    return strat


def pead_equal_weight(members: Mapping[str, MembershipRow], earnings: EarningsView,
                      *, variant: str = VARIANT) -> PortfolioStrategy:
    """Informational benchmark: equal weight over ALL point-in-time eligible
    names (those with a live signal), monthly (NOT binding)."""
    def strat(view: PanelView) -> dict[str, float]:
        elig = pead_pit_eligible(view, earnings, members, variant=variant)
        if not elig:
            return {}
        w = 1.0 / len(elig)
        return {s: w for s in elig}
    return strat


def pead_null_results(panel: PricePanel, members: Mapping[str, MembershipRow],
                      earnings: EarningsView, *, costs: CostModel, start: date,
                      paths: int, seed: int, variant: str = VARIANT,
                      ) -> list[PortfolioResult]:
    """Seeded monkey portfolios (ADR-0002 #2): at each rebalance the SAME COUNT
    of names the strategy would hold, drawn uniformly without replacement from
    the SAME point-in-time PEAD-eligible set (names that had a fresh surprise),
    equal weight, through the IDENTICAL delisting-aware engine. If ranking by
    SUE cannot beat dart-throwing among recently-reported names, the ranking
    carries no information. One rng drives all paths sequentially; eligible sets
    cached per rebalance index. Full results kept so the endpoint exhibit
    truncates the stored curves exactly."""
    rng = random.Random(seed)
    cache: dict[int, list[str]] = {}

    def monkey(view: PanelView) -> dict[str, float]:
        elig = cache.get(view.t)
        if elig is None:
            elig = pead_pit_eligible(view, earnings, members, variant=variant)
            cache[view.t] = elig
        if not elig:
            return {}
        pick = rng.sample(elig, min(winner_count(len(elig)), len(elig)))
        w = 1.0 / len(pick)
        return {s: w for s in pick}

    return [run_pit_backtest(panel, monkey, costs, start=start).result
            for _ in range(paths)]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PeadPitRun:
    universe: PitUniverse
    coverage: PeadCoverage
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
    member_counts: list[tuple[date, int, int]]   # (rebalance, members, eligible)
    family: str = FAMILY
    return_convention: str = PRICE_CONVENTION
    null_results: tuple[PortfolioResult, ...] = ()
    n_paths: int = 0


def run_pead_pit(session: Session, audit: PostgresAuditLog, *,
                 paths: int = 1000, seed: int = 7, total_return: bool = False,
                 window_start: date | None = None,
                 variant: str = VARIANT) -> PeadPitRun:
    if window_start is not None and not total_return:
        raise ValueError("window_start overrides are pre-committed board tests "
                         "and exist only in total-return mode")
    if window_start is not None and window_start <= WINDOW_START:
        raise ValueError(f"window_start {window_start} must be after the "
                         f"membership-reliability bound {WINDOW_START}")
    family = tr_family(window_start) if total_return else FAMILY
    convention = TR_CONVENTION if total_return else PRICE_CONVENTION
    universe = load_pit_panel(session, total_return=total_return)
    panel, members = universe.panel, universe.members
    earnings, coverage = load_pead_signals(
        session, sorted(members), panel.dates, members)

    eval_start = WINDOW_START if window_start is None else window_start
    start_i = bisect_left(panel.dates, eval_start)
    if start_i >= len(panel.dates):
        raise RuntimeError(f"panel ends before the evaluation start {eval_start}")
    start = panel.dates[start_i]
    strategy = pead_pit_strategy(members, earnings, variant=variant)

    pit = run_pit_backtest(panel, strategy, COSTS, start=start)
    result = pit.result

    member_counts: list[tuple[date, int, int]] = []
    for t in month_end_indices(panel.dates, start_i, len(panel.dates)):
        day = panel.dates[t]
        n_members = sum(1 for r in universe.window_rows if is_member_on(r, day))
        n_elig = len(pead_pit_eligible(PanelView(panel, t), earnings, members,
                                       variant=variant))
        member_counts.append((day, n_members, n_elig))

    trials_before_total = total_trial_count(session)
    spec: dict[str, object] = {
        **SPEC, "family": family, "signal_variant": variant,
        "universe": f"point-in-time {INDEX_CODE} membership "
                    "(validation.index_membership, fail-closed interval rule)",
        "return_convention": convention,
        "window_start": str(WINDOW_START), "evaluation_start": str(eval_start),
        "window": f"{panel.dates[0]}..{panel.dates[-1]}", "start": str(start),
        "top_n": f"winner decile: max(10, n_eligible // {DECILE})",
        "members_with_series": len(members),
        "symbols_with_reports": coverage.symbols_with_reports,
        "total_reports": coverage.total_reports,
        "delisting_rule": "liquidate at final available close, per-side cost, "
                          "proceeds in cash until next rebalance",
        "data": "EODHD real", "no_lookahead": "structural (report_date gates the "
        "signal; after-market prints tradable next session; future reports "
        "physically excluded)",
        "costs_bps_per_side": COSTS.commission_bps + COSTS.slippage_bps}
    trial_id = register_trial(
        session, family=family, spec=spec,
        metrics={"total_return": result.total_return, "sharpe": result.sharpe,
                 "max_drawdown": result.max_drawdown,
                 "avg_turnover": result.avg_turnover,
                 "n_rebalances": float(result.n_rebalances)})
    n_trials = trial_count(session, family)
    trials_after_total = total_trial_count(session)

    null_results = tuple(pead_null_results(panel, members, earnings, costs=COSTS,
                                           start=start, paths=paths, seed=seed,
                                           variant=variant))
    nulls = [r.total_return for r in null_results]
    spy = run_pit_backtest(panel, buy_and_hold_strategy(BENCHMARK), COSTS,
                           start=start).result
    ew = run_pit_backtest(panel, pead_equal_weight(members, earnings,
                                                   variant=variant),
                          COSTS, start=start).result
    gate = portfolio_gate(result=result, null_returns=nulls, spy=spy, ew=ew,
                          n_trials=n_trials)
    wf = pit_walk_forward(panel, strategy, k=K_FOLDS, horizon=HORIZON,
                          embargo=EMBARGO, warmup=start_i, costs=COSTS)

    audit.append(
        event_type="quant.backtest.completed", entity_type="strategy",
        entity_id=f"{family}/portfolio", actor_type="dcp",
        actor_id="pead_pit_run",
        payload={"universe": f"point-in-time {INDEX_CODE}",
                 "signal": "SUE (Foster-Olsen-Shevlin); PEAD",
                 "signal_variant": variant, "return_convention": convention,
                 "members_with_series": len(members),
                 "symbols_with_reports": coverage.symbols_with_reports,
                 "symbols_with_signal": coverage.symbols_with_signal,
                 "total_reports": coverage.total_reports,
                 "trial_id": trial_id, "n_trials": n_trials,
                 "window": f"{panel.dates[0]}..{panel.dates[-1]}",
                 "start": str(start), "gate_passed": gate.passed,
                 "gate_reasons": list(gate.reasons), "null_p": gate.null_p_value,
                 "dsr": gate.dsr, "spy_bh_return": gate.spy_bh_return,
                 "ew_return": gate.ew_return,
                 "forced_liquidations": len(pit.forced_liquidations),
                 "unfilled_buys": len(pit.unfilled_buys),
                 "avg_turnover": result.avg_turnover,
                 "n_rebalances": result.n_rebalances,
                 "wf_positive_folds": wf.positive_folds,
                 "no_lookahead": "structural (signals/pead/v1.py)"})
    return PeadPitRun(
        universe=universe, coverage=coverage, start=start, run=pit, spy=spy,
        ew=ew, gate=gate, wf=wf, trial_id=trial_id, n_trials=n_trials,
        trials_before_total=trials_before_total,
        trials_after_total=trials_after_total, member_counts=member_counts,
        family=family, return_convention=convention, null_results=null_results,
        n_paths=paths)


# ---------------------------------------------------------------------------
# Verdict-vs-endpoint exhibit (exact truncation of the stored curves — the same
# construction as xsmom_pit, reusing its _return_at/_sharpe_at helpers)
# ---------------------------------------------------------------------------

def verdict_vs_endpoint(run: PeadPitRun, *,
                        months: int = ENDPOINT_MONTHS) -> list[EndpointVerdict]:
    if not run.null_results:
        raise ValueError("verdict_vs_endpoint needs stored null curves")
    dates = run.run.result.dates
    if run.spy.dates != dates or any(r.dates != dates for r in run.null_results):
        raise RuntimeError("strategy/SPY/null curves cover different sessions")
    month_ends = [i for i in range(len(dates) - 1)
                  if dates[i].month != dates[i + 1].month]
    endpoints = month_ends[-months:] + [len(dates) - 1]
    out: list[EndpointVerdict] = []
    for idx in endpoints:
        sr = _return_at(run.run.result, idx)
        spy_r = _return_at(run.spy, idx)
        p = (sum(1 for nr in run.null_results if _return_at(nr, idx) >= sr)
             / len(run.null_results))
        dsr = deflated_sharpe(_sharpe_at(run.run.result, idx), idx, run.n_trials)
        beats = sr > spy_r
        out.append(EndpointVerdict(
            endpoint=dates[idx], strategy_return=sr, spy_return=spy_r,
            null_p=p, dsr=dsr, beats_spy=beats,
            passed=beats and p <= P_MAX and dsr >= DSR_MIN))
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _annual_distribution_lines(run: PeadPitRun) -> list[str]:
    lines = ["## Annual outcome distribution", ""]
    if not run.gate.passed:
        lines += ["No distribution is derived for a failed strategy (house rule: "
                  "earnings profiles are derived only for validated strategies — "
                  "profit is a result to be discovered, never an input).", ""]
        return lines
    lines += [
        "> **History is not a forecast.** This is the DISPERSION a strategy like "
        "this has exhibited; the median is not a promise.", "",
        "| year | strategy | SPY B&H | note |", "|---|---|---|---|"]
    strat_years = calendar_year_returns(run.run.result)
    spy_years = {y.year: y for y in calendar_year_returns(run.spy)}
    for y in strat_years:
        lines.append(f"| {y.year} | {y.ret:+.2%} | {spy_years[y.year].ret:+.2%} "
                     f"| {y.note} |")
    strat_draws = block_bootstrap_annual(daily_returns(run.run.result))
    spy_draws = block_bootstrap_annual(daily_returns(run.spy))
    lines += [
        "",
        f"Block bootstrap: daily returns resampled in {BOOT_BLOCK}-session "
        f"blocks, {BOOT_DRAWS} seeded draws of {BOOT_HORIZON} sessions (seed "
        f"{BOOT_SEED}); paired draws, same method both columns.", "",
        "| percentile of simulated annual return | strategy | SPY B&H |",
        "|---|---|---|",
        *[f"| {label} | {percentile(strat_draws, q):+.2%} "
          f"| {percentile(spy_draws, q):+.2%} |" for label, q in _PCTS], ""]
    return lines


def render_pead_report(run: PeadPitRun, *, paths: int,
                       endpoints: list[EndpointVerdict] | None = None) -> str:
    panel, g, wf, r = run.universe.panel, run.gate, run.wf, run.run.result
    cov = run.coverage
    verdict = "PASS" if g.passed else "FAIL"
    implication = (
        "earnings-surprise (SUE/PEAD) on the point-in-time S&P 500 — dead "
        "companies included — clears the full gauntlet: a SECOND, orthogonal "
        "alpha source is validated on honest membership"
        if g.passed else
        "SUE/PEAD does not clear the fund's bar on honest point-in-time "
        "membership; the graveyard verdict is recorded verbatim and the factor "
        "must not proceed toward approval (a failed gate is a deliverable, not "
        "a defect to be tuned away)")
    fold_rets = ", ".join(f"{x.total_return:+.2%}" for x in wf.fold_results)
    yearly = [mc for mc in run.member_counts if mc[0].month == 12]

    lines = [
        f"# THE EARNINGS-SURPRISE TEST — SUE/PEAD on the point-in-time S&P 500 "
        f"({run.return_convention.split(' ')[0]})",
        "",
        "> ## THE ONE ORTHOGONAL FACTOR THROUGH THE IDENTICAL BAR",
        "> The ONLY difference from the momentum run is the signal: names are "
        "ranked by",
        "> Standardized Unexpected Earnings (Foster-Olsen-Shevlin SUE), not "
        "12-1 price",
        "> momentum. Universe, delisting-aware engine, top-decile equal-weight "
        "monthly",
        "> construction, monkey null, deflated Sharpe, purged walk-forward and "
        "the binding",
        "> beat-SPY bar are REUSED BY IMPORT from xsmom_pit_run and the "
        "committed gauntlet.",
        "",
        "> ## NO LOOK-AHEAD IS STRUCTURAL (signals/pead/v1.py)",
        "> The report_date gates when a surprise is knowable; an after-market "
        "print is",
        "> tradable only the NEXT session; the standardization of a report uses "
        "ONLY",
        "> strictly-prior reports; and a report dated after the decision "
        "session is",
        "> physically excluded from the ranking. A future report's numbers can "
        "be flipped",
        "> wildly and the ranking at t is byte-identical (pinned by test).",
        "",
        f"Pinned spec (textbook, zero search): SUE = (epsActual - epsEstimate) / "
        f"stdev(surprise over the prior {STANDARDIZE_WINDOW} reported quarters); "
        f">= {STANDARDIZE_MIN} priors required (else ineligible); drift-capture "
        f"staleness window {STALENESS_SESSIONS} sessions; EPS split-adjusted on "
        f"read (house convention, keyed on report_date). Winner portfolio is "
        f"the top decile (max(10, n_eligible // {DECILE})), equal weight, "
        "monthly.",
        "",
        f"- Evaluation window STARTS {WINDOW_START} (membership-reliability "
        f"bound); costs {COSTS.commission_bps}+{COSTS.slippage_bps} bps/side; "
        f"null {paths}-path monkey MC drawing from the SAME PEAD-eligible set; "
        f"purged walk-forward k={K_FOLDS}, horizon={HORIZON}, embargo={EMBARGO}; "
        "one registered trial per family; deflated Sharpe at the true count.",
        f"- Binding benchmark: SPY {'total return' if 'total' in run.return_convention else 'buy-and-hold'} "
        "over the same window (ADR-0009); SPY carries no membership row and can "
        "never be ranked. Equal-weight-all-eligible shown, NOT binding.",
        "",
        "## Data quality and honesty — earnings coverage",
        "",
        f"- Panel members with a usable price series: {cov.symbols_in_panel}",
        f"- Members carrying >= 1 stored surprise: {cov.symbols_with_reports} "
        f"({cov.total_reports} completed reports on record; "
        f"{cov.delisted_with_reports} of them delisted names — "
        "survivorship-free)",
        f"- Members that ever produce a standardizable SUE in-window: "
        f"{cov.symbols_with_signal}",
        "- Members/eligible at each December rebalance: "
        + "; ".join(f"{d.year}: {m}/{e}" for d, m, e in yearly),
        f"- Forced delisting liquidations during the run: "
        f"{len(run.run.forced_liquidations)}; unfilled buys (died between "
        f"decision and execution): {len(run.run.unfilled_buys)}",
        "",
        f"## Full-window result (start {run.start}, panel {panel.dates[0]} → "
        f"{panel.dates[-1]}, {len(panel.dates)} aligned XNYS sessions, "
        f"{run.return_convention})",
        "",
        f"Return {r.total_return:+.2%}, Sharpe {r.sharpe:.2f}, max drawdown "
        f"{r.max_drawdown:.2%}, avg turnover {r.avg_turnover:.2%} per rebalance, "
        f"{r.n_rebalances} rebalances",
        "",
        f"### Gate verdict: **{verdict}**",
        "",
        f"- verdict: **{verdict}**",
        f"- implication: {implication}",
        f"- strategy return: {g.strategy_return:+.2%}",
        f"- SPY (BINDING benchmark per ADR-0009): {g.spy_bh_return:+.2%}",
        f"- equal-weight all-eligible (informational, NOT binding): "
        f"{g.ew_return:+.2%}",
        f"- null-model p-value: {g.null_p_value:.3f} (must be <= {P_MAX})",
        f"- deflated Sharpe: {g.dsr:.3f} at n_trials={g.n_trials} "
        f"(must be >= {DSR_MIN})",
        f"- trial registry id: `{run.trial_id}` (family `{run.family}`)",
        "",
    ]
    if g.reasons:
        lines.append("Verbatim gate reasons:")
        lines += [f"- {reason}" for reason in g.reasons]
        lines.append("")
    lines += [
        f"### Walk-forward: {wf.positive_folds}/{len(wf.fold_results)} folds "
        "positive",
        "",
        f"- fold returns: {fold_rets}",
        f"- mean return {wf.mean_return:+.2%}, mean Sharpe {wf.mean_sharpe:.2f}, "
        f"worst fold {wf.worst_fold_return:+.2%}",
        "",
    ]
    if endpoints is not None:
        n_pass = sum(1 for e in endpoints if e.passed)
        n_beat = sum(1 for e in endpoints if e.beats_spy)
        lines += [
            f"### Exhibit: verdict vs endpoint ({run.return_convention.split(' ')[0]})",
            "",
            f"The identical run re-judged at the final date and each of the "
            f"prior {ENDPOINT_MONTHS} month-ends (exact truncation of the stored "
            "curves). A robust edge survives the choice of endpoint.",
            "",
            f"- endpoints passing the full gate: {n_pass}/{len(endpoints)}; "
            f"beating SPY: {n_beat}/{len(endpoints)}",
            "",
            "| endpoint | strategy | SPY | null p | DSR | beats SPY | PASS |",
            "|---|---|---|---|---|---|---|",
            *[f"| {e.endpoint} | {e.strategy_return:+.2%} | {e.spy_return:+.2%} "
              f"| {e.null_p:.3f} | {e.dsr:.3f} | {'yes' if e.beats_spy else 'no'} "
              f"| {'PASS' if e.passed else 'FAIL'} |" for e in endpoints],
            "",
        ]
    lines += [
        "## Summary",
        "",
        "| strategy | return | SPY | EW eligible | Sharpe | max DD | turnover "
        "| rebalances | null p | DSR (n) | WF + | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        f"| SUE/PEAD, PIT S&P 500 winner decile | {r.total_return:+.2%} "
        f"| {g.spy_bh_return:+.2%} | {g.ew_return:+.2%} | {r.sharpe:.2f} "
        f"| {r.max_drawdown:.2%} | {r.avg_turnover:.2%} | {r.n_rebalances} "
        f"| {g.null_p_value:.3f} | {g.dsr:.3f} ({g.n_trials}) "
        f"| {wf.positive_folds}/{len(wf.fold_results)} | **{verdict}** |",
        "",
        f"Trial registry: **{run.trials_before_total} → {run.trials_after_total}** "
        f"(one `{run.family}` trial; family count now {run.n_trials}).",
        "",
        *_annual_distribution_lines(run),
        "## Approval status",
        "",
        "**None sought here — by design.** This is a VALIDATION run on a "
        "membership-gated universe of validation-only instruments "
        "(is_active=FALSE); it settles whether SUE/PEAD is a real, orthogonal "
        "alpha source on honest membership. It does not itself qualify any "
        "strategy for the approval workflow. Gates were not modified; no "
        "strategy row is touched.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(description="SUE/PEAD on the point-in-time S&P 500")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--total-return", action="store_true")
    p.add_argument("--report", type=Path)
    a = p.parse_args()

    with session_scope() as s:
        last_bar = s.execute(text(
            "SELECT max(bar_date) FROM market.price_bars_daily "
            "WHERE source='EodhdAdapter'")).scalar()
        if last_bar is None:
            raise SystemExit("no real bars in the database — run the backfill first")
        clock = FrozenClock(datetime(last_bar.year, last_bar.month, last_bar.day,
                                     22, 0, tzinfo=UTC))
        audit = PostgresAuditLog(s, clock)
        run = run_pead_pit(s, audit, paths=a.paths, total_return=a.total_return)
        endpoints = (verdict_vs_endpoint(run) if a.total_return else None)
        report = render_pead_report(run, paths=a.paths, endpoints=endpoints)
        if a.total_return:
            kill = run_pead_pit(s, audit, paths=a.paths, total_return=True,
                                window_start=KILL_START)
            report += ("\n\n---\n\n## Pre-committed KILL-ONLY trial "
                       f"(start {KILL_START}, family {kill.family})\n\n"
                       + render_pead_report(kill, paths=a.paths))
        default = TR_REPORT if a.total_return else PRICE_REPORT
        out = a.report or default
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report)

    print(f"pead-sue: gate={'PASS' if run.gate.passed else 'FAIL'} "
          f"return={run.run.result.total_return:+.2%} "
          f"spy={run.gate.spy_bh_return:+.2%} p={run.gate.null_p_value:.3f} "
          f"dsr={run.gate.dsr:.3f} wf={run.wf.positive_folds}/"
          f"{len(run.wf.fold_results)} (reasons: "
          f"{list(run.gate.reasons) or 'none'})")
    print(f"report written: {out}")


if __name__ == "__main__":
    main()
