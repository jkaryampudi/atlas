"""THE QUALITY TEST: Novy-Marx gross profitability (GP/A) on the POINT-IN-TIME
S&P 500 — strategy candidate #3 through the IDENTICAL bar momentum and PEAD
faced. The ONLY difference versus xsmom-pit / pead-pit is the signal: names are
ranked by trailing-four-quarter gross profit over most-recent total assets
(Novy-Marx 2013; signals.quality.v1) instead of 12-1 price momentum or SUE.
Everything else — the point-in-time membership universe (INCLUDING delisted
names), the delisting-aware engine, the top-decile equal-weight monthly
construction, the 1000-path monkey null, the deflated Sharpe at the TRUE
registered trial count, the purged+embargoed walk-forward, the binding
beat-SPY-total-return bar, and the 10 bps/side costs — is REUSED BY IMPORT
from xsmom_pit_run and the committed portfolio gauntlet, never restated.

WHY THIS FACTOR. Quality is the canonical low-turnover diversifier to price
momentum and earnings surprise: it reads the income statement's cleanest
profitability line against the balance sheet, moves on filings (quarterly) not
prices (daily), and the textbook construction has zero free parameters. It is
also a factor long-only implementations often FAIL to monetize against a
raging benchmark — a graveyard FAIL recorded verbatim is an acceptable and
expected outcome, not a defect.

NO LOOK-AHEAD is STRUCTURAL and lives in the signal (signals/quality/v1.py):
filing_date gates when a quarter's figures become knowable (a filing is
tradable the NEXT session), a quarter's GP/A uses the LATEST filing among its
four inputs, and a filing dated after the decision session physically cannot
enter the ranking. Vendor rows with filing_date <= period end (a probed
defect: AVGO stamps 46/78 income quarters that way) never enter storage —
market_data/quarterly_fundamentals.py drops them fail-closed.

FINANCIALS NOTE (honesty). Novy-Marx 2013 EXCLUDES financial firms: banks and
insurers carry big low-gross-margin balance sheets and structurally low GP/A,
so a full-universe long-only top decile will simply never hold them. The
DEFAULT run here is the FULL point-in-time S&P 500 — no silent exclusion. The
--exclude-financials flag implements the paper's universe as a SECOND
registered trial family ('-xfin' suffix, sector_gics = 'Financials' from
market.instruments); it is OFF by default and the orchestrator decides whether
to spend that trial.

TOTAL-RETURN MODE (--total-return): ADR-0009's binding benchmark is SPY TOTAL
RETURN. The panel is transformed at load time (dividends reinvested at the
ex-date close, applied identically to strategy, monkey null, EW and SPY), TWO
trials run — family 'quality-gpa-tr' (identical window) and the pre-committed
KILL-ONLY 'quality-gpa-tr-2016' (later start; can only demote) — and the
verdict-vs-endpoint exhibit rolls the final date back monthly.

Do NOT tune anything to pass. A failed gate is a valid, reportable result; the
graveyard verdict recorded verbatim IS the deliverable.

Usage: python -m atlas.dcp.backtest.quality_pit_run [--paths 1000]
       python -m atlas.dcp.backtest.quality_pit_run --total-return [--paths 1000]
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
from atlas.dcp.backtest.registry import lineage_count, register_trial
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
from atlas.dcp.market_data.index_membership import (
    INDEX_CODE,
    WINDOW_START,
    MembershipRow,
    is_member_on,
)
from atlas.dcp.market_data.quarterly_fundamentals import QuarterlyFundamentals
from atlas.dcp.signals.quality.v1 import (
    CONSECUTIVE_SPAN_DAYS,
    SPEC,
    STALENESS_SESSIONS,
    TRAILING_QUARTERS,
    FundamentalsView,
    build_fundamentals_view,
    quality_eligible,
)

ROOT = Path(__file__).resolve().parents[3]
FAMILY = "quality-gpa"
FAMILY_TR = "quality-gpa-tr"
# ADR-0016: every quality-* family is the quality research line — deflated
# Sharpe counts the full lineage; a new family name never resets the penalty.
LINEAGE = "quality"
KILL_START = date(2016, 1, 1)         # pre-committed kill-only TR start (demote only)
FINANCIALS_SECTOR = "Financials"      # sector_gics label for the -xfin variant
PRICE_CONVENTION = "price (split-adjusted; dividends not reinvested)"
TR_CONVENTION = ("total return (split-adjusted; each dividend reinvested at its "
                 "ex-date close — market_data/total_return.py)")
PRICE_REPORT = ROOT / "docs" / "reports" / "quality-gpa-pit-sp500.md"
TR_REPORT = ROOT / "docs" / "reports" / "quality-gpa-pit-total-return.md"


def family_name(*, total_return: bool, exclude_financials: bool,
                window_start: date | None) -> str:
    """Registered family: base 'quality-gpa' / 'quality-gpa-tr', '-xfin' when
    the paper's financials exclusion is on (a SECOND trial, never silent), and
    '-<year>' for a pre-committed kill-only start override."""
    fam = FAMILY_TR if total_return else FAMILY
    if exclude_financials:
        fam += "-xfin"
    if window_start is not None:
        fam += f"-{window_start.year}"
    return fam


# ---------------------------------------------------------------------------
# Fundamentals panel loading (from market.quarterly_fundamentals)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityCoverage:
    symbols_in_panel: int
    symbols_with_fundamentals: int   # panel members carrying any stored quarter
    total_quarters: int
    symbols_with_signal: int         # symbols with >= 1 in-window signal event
    delisted_with_fundamentals: int


def load_quality_signals(session: Session, symbols: list[str], dates: list[date],
                         members: Mapping[str, MembershipRow],
                         ) -> tuple[FundamentalsView, QualityCoverage]:
    """Build the point-in-time FundamentalsView over the panel's session
    calendar from market.quarterly_fundamentals (only anchorable quarters are
    ever stored — filing_date strictly after the period end by CHECK
    constraint). Coverage is reported honestly."""
    rows: dict[str, list[QuarterlyFundamentals]] = {}
    for r in session.execute(text(
            "SELECT i.symbol, qf.fiscal_period_end, qf.filing_date, "
            "       qf.gross_profit, qf.total_revenue, qf.total_assets, "
            "       qf.currency "
            "FROM market.quarterly_fundamentals qf "
            "JOIN market.instruments i ON i.id = qf.instrument_id "
            "WHERE i.symbol = ANY(:syms) "
            "ORDER BY i.symbol, qf.fiscal_period_end"), {"syms": symbols}).all():
        rows.setdefault(r.symbol, []).append(QuarterlyFundamentals(
            symbol=r.symbol, fiscal_period_end=r.fiscal_period_end,
            filing_date=r.filing_date,
            gross_profit=(Decimal(r.gross_profit)
                          if r.gross_profit is not None else None),
            total_revenue=(Decimal(r.total_revenue)
                           if r.total_revenue is not None else None),
            total_assets=(Decimal(r.total_assets)
                          if r.total_assets is not None else None),
            currency=r.currency))
    view = build_fundamentals_view(rows, dates)
    delisted_with = sum(1 for s in rows
                        if members.get(s) is not None and members[s].is_delisted)
    cov = QualityCoverage(
        symbols_in_panel=len(symbols),
        symbols_with_fundamentals=len(rows),
        total_quarters=sum(len(v) for v in rows.values()),
        symbols_with_signal=len(view.symbols()),
        delisted_with_fundamentals=delisted_with)
    return view, cov


def financial_symbols(session: Session, symbols: list[str]) -> frozenset[str]:
    """Symbols classified sector_gics = 'Financials' in market.instruments —
    the -xfin variant's exclusion set. Names with a NULL sector are NOT
    excluded (unclassifiable; the report states the count when the flag is
    on, so the approximation is visible, never silent)."""
    return frozenset(r.symbol for r in session.execute(text(
        "SELECT symbol FROM market.instruments "
        "WHERE symbol = ANY(:syms) AND sector_gics = :sec"),
        {"syms": symbols, "sec": FINANCIALS_SECTOR}))


# ---------------------------------------------------------------------------
# Point-in-time quality eligibility, strategy, benchmarks, null — the ONLY
# difference vs xsmom_pit/pead_pit is the signal; the engine is imported
# unchanged.
# ---------------------------------------------------------------------------

def quality_pit_eligible(view: PanelView, fundamentals: FundamentalsView,
                         members: Mapping[str, MembershipRow], *,
                         excluded: frozenset[str] = frozenset()) -> list[str]:
    """Index member on the view's date (fail-closed interval rule) AND a live,
    fresh, defined GP/A AND a price at t, minus the explicit exclusion set
    (empty by default; the -xfin variant's financials). Shared verbatim by
    strategy, monkey null and the equal-weight benchmark, so all face the
    identical universe by construction."""
    today = view.today
    out: list[str] = []
    for s in quality_eligible(view, fundamentals):
        if s in excluded:
            continue
        row = members.get(s)
        if row is None or not is_member_on(row, today):
            continue
        out.append(s)
    return out


def quality_pit_strategy(members: Mapping[str, MembershipRow],
                         fundamentals: FundamentalsView, *,
                         excluded: frozenset[str] = frozenset(),
                         ) -> PortfolioStrategy:
    """Rank the point-in-time eligible set by live GP/A DESCENDING, hold the
    winner decile equal-weight (winner_count imported from xsmom_pit for
    apples-to-apples), deterministic symbol tie-break."""
    def strat(view: PanelView) -> dict[str, float]:
        t = view.t
        ranked: list[tuple[float, str]] = []
        for s in quality_pit_eligible(view, fundamentals, members,
                                      excluded=excluded):
            sig = fundamentals.live(s, t)
            assert sig is not None  # quality_pit_eligible guarantees it
            ranked.append((sig, s))
        ranked.sort(key=lambda rs: (-rs[0], rs[1]))
        top = ranked[:winner_count(len(ranked))]
        if not top:
            return {}
        w = 1.0 / len(top)
        return {s: w for _, s in top}
    return strat


def quality_equal_weight(members: Mapping[str, MembershipRow],
                         fundamentals: FundamentalsView, *,
                         excluded: frozenset[str] = frozenset(),
                         ) -> PortfolioStrategy:
    """Informational benchmark: equal weight over ALL point-in-time eligible
    names (those with a live GP/A), monthly (NOT binding)."""
    def strat(view: PanelView) -> dict[str, float]:
        elig = quality_pit_eligible(view, fundamentals, members,
                                    excluded=excluded)
        if not elig:
            return {}
        w = 1.0 / len(elig)
        return {s: w for s in elig}
    return strat


def quality_null_results(panel: PricePanel, members: Mapping[str, MembershipRow],
                         fundamentals: FundamentalsView, *, costs: CostModel,
                         start: date, paths: int, seed: int,
                         excluded: frozenset[str] = frozenset(),
                         ) -> list[PortfolioResult]:
    """Seeded monkey portfolios (ADR-0002 #2): at each rebalance the SAME COUNT
    of names the strategy would hold, drawn uniformly without replacement from
    the SAME point-in-time GP/A-eligible set (names with a live, fresh, defined
    quality signal), equal weight, through the IDENTICAL delisting-aware
    engine. If ranking by GP/A cannot beat dart-throwing among
    fundamentals-covered names, the ranking carries no information. One rng
    drives all paths sequentially; eligible sets cached per rebalance index.
    Full results kept so the endpoint exhibit truncates the stored curves
    exactly."""
    rng = random.Random(seed)
    cache: dict[int, list[str]] = {}

    def monkey(view: PanelView) -> dict[str, float]:
        elig = cache.get(view.t)
        if elig is None:
            elig = quality_pit_eligible(view, fundamentals, members,
                                        excluded=excluded)
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
class QualityPitRun:
    universe: PitUniverse
    coverage: QualityCoverage
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
    excluded_financials: tuple[str, ...] = ()    # -xfin only; empty by default
    members_null_sector: int = 0                 # unclassifiable when flag on
    lineage: str = LINEAGE


def run_quality_pit(session: Session, audit: PostgresAuditLog, *,
                    paths: int = 1000, seed: int = 7, total_return: bool = False,
                    window_start: date | None = None,
                    exclude_financials: bool = False) -> QualityPitRun:
    if window_start is not None and not total_return:
        raise ValueError("window_start overrides are pre-committed board tests "
                         "and exist only in total-return mode")
    if window_start is not None and window_start <= WINDOW_START:
        raise ValueError(f"window_start {window_start} must be after the "
                         f"membership-reliability bound {WINDOW_START}")
    family = family_name(total_return=total_return,
                         exclude_financials=exclude_financials,
                         window_start=window_start)
    convention = TR_CONVENTION if total_return else PRICE_CONVENTION
    universe = load_pit_panel(session, total_return=total_return)
    panel, members = universe.panel, universe.members
    fundamentals, coverage = load_quality_signals(
        session, sorted(members), panel.dates, members)
    excluded = (financial_symbols(session, sorted(members))
                if exclude_financials else frozenset())
    null_sector = 0
    if exclude_financials:
        null_sector = int(session.execute(text(
            "SELECT count(*) FROM market.instruments "
            "WHERE symbol = ANY(:syms) AND sector_gics IS NULL"),
            {"syms": sorted(members)}).scalar() or 0)

    eval_start = WINDOW_START if window_start is None else window_start
    start_i = bisect_left(panel.dates, eval_start)
    if start_i >= len(panel.dates):
        raise RuntimeError(f"panel ends before the evaluation start {eval_start}")
    start = panel.dates[start_i]
    strategy = quality_pit_strategy(members, fundamentals, excluded=excluded)

    pit = run_pit_backtest(panel, strategy, COSTS, start=start)
    result = pit.result

    member_counts: list[tuple[date, int, int]] = []
    for t in month_end_indices(panel.dates, start_i, len(panel.dates)):
        day = panel.dates[t]
        n_members = sum(1 for r in universe.window_rows if is_member_on(r, day))
        n_elig = len(quality_pit_eligible(PanelView(panel, t), fundamentals,
                                          members, excluded=excluded))
        member_counts.append((day, n_members, n_elig))

    trials_before_total = total_trial_count(session)
    spec: dict[str, object] = {
        **SPEC, "family": family,
        "universe": f"point-in-time {INDEX_CODE} membership "
                    "(validation.index_membership, fail-closed interval rule)"
                    + (" MINUS sector_gics='Financials' (the paper's exclusion, "
                       "explicit -xfin trial)" if exclude_financials else
                       " (FULL universe incl. financials; Novy-Marx 2013 "
                       "excludes them — documented, not silently applied)"),
        "financials_excluded": exclude_financials,
        "n_financials_excluded": len(excluded),
        "return_convention": convention,
        "window_start": str(WINDOW_START), "evaluation_start": str(eval_start),
        "window": f"{panel.dates[0]}..{panel.dates[-1]}", "start": str(start),
        "top_n": f"winner decile: max(10, n_eligible // {DECILE})",
        "members_with_series": len(members),
        "symbols_with_fundamentals": coverage.symbols_with_fundamentals,
        "total_quarters": coverage.total_quarters,
        "delisting_rule": "liquidate at final available close, per-side cost, "
                          "proceeds in cash until next rebalance",
        "data": "EODHD real", "no_lookahead": "structural (filing_date gates the "
        "signal; a filing is tradable the next session; quarters with "
        "filing_date <= period end never stored; future filings physically "
        "excluded)",
        "costs_bps_per_side": COSTS.commission_bps + COSTS.slippage_bps}
    trial_id = register_trial(
        session, family=family, lineage=LINEAGE, spec=spec,
        metrics={"total_return": result.total_return, "sharpe": result.sharpe,
                 "max_drawdown": result.max_drawdown,
                 "avg_turnover": result.avg_turnover,
                 "n_rebalances": float(result.n_rebalances)})
    n_trials = lineage_count(session, LINEAGE)
    trials_after_total = total_trial_count(session)

    null_results = tuple(quality_null_results(
        panel, members, fundamentals, costs=COSTS, start=start, paths=paths,
        seed=seed, excluded=excluded))
    nulls = [r.total_return for r in null_results]
    spy = run_pit_backtest(panel, buy_and_hold_strategy(BENCHMARK), COSTS,
                           start=start).result
    ew = run_pit_backtest(panel, quality_equal_weight(members, fundamentals,
                                                      excluded=excluded),
                          COSTS, start=start).result
    gate = portfolio_gate(result=result, null_returns=nulls, spy=spy, ew=ew,
                          n_trials=n_trials)
    wf = pit_walk_forward(panel, strategy, k=K_FOLDS, horizon=HORIZON,
                          embargo=EMBARGO, warmup=start_i, costs=COSTS)

    audit.append(
        event_type="quant.backtest.completed", entity_type="strategy",
        entity_id=f"{family}/portfolio", actor_type="dcp",
        actor_id="quality_pit_run",
        payload={"universe": f"point-in-time {INDEX_CODE}",
                 "signal": "GP/A (Novy-Marx gross profitability)",
                 "financials_excluded": exclude_financials,
                 "n_financials_excluded": len(excluded),
                 "return_convention": convention,
                 "members_with_series": len(members),
                 "symbols_with_fundamentals": coverage.symbols_with_fundamentals,
                 "symbols_with_signal": coverage.symbols_with_signal,
                 "total_quarters": coverage.total_quarters,
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
                 "no_lookahead": "structural (signals/quality/v1.py)"})
    return QualityPitRun(
        universe=universe, coverage=coverage, start=start, run=pit, spy=spy,
        ew=ew, gate=gate, wf=wf, trial_id=trial_id, n_trials=n_trials,
        trials_before_total=trials_before_total,
        trials_after_total=trials_after_total, member_counts=member_counts,
        family=family, return_convention=convention, null_results=null_results,
        n_paths=paths, excluded_financials=tuple(sorted(excluded)),
        members_null_sector=null_sector, lineage=LINEAGE)


# ---------------------------------------------------------------------------
# Verdict-vs-endpoint exhibit (exact truncation of the stored curves — the same
# construction as xsmom_pit, reusing its _return_at/_sharpe_at helpers)
# ---------------------------------------------------------------------------

def verdict_vs_endpoint(run: QualityPitRun, *,
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
# Report — honest-verdict discipline: no robustness boilerplate; a PASS is a
# gate result only; a FAIL is the graveyard verdict verbatim (the PEAD audit
# lesson: the renderer must never carry claims its own exhibits refute).
# ---------------------------------------------------------------------------

def _annual_distribution_lines(run: QualityPitRun) -> list[str]:
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


def render_quality_report(run: QualityPitRun, *, paths: int,
                          endpoints: list[EndpointVerdict] | None = None) -> str:
    panel, g, wf, r = run.universe.panel, run.gate, run.wf, run.run.result
    cov = run.coverage
    verdict = "PASS" if g.passed else "FAIL"
    implication = (
        "gross profitability (GP/A) on the point-in-time S&P 500 — dead "
        "companies included — clears the binding full-window beat-SPY gate. "
        "This is the gate result ONLY; robustness (endpoint concentration, the "
        "pre-committed kill-only trial, and orthogonality to existing factors) "
        "must be weighed separately before approval — a full-window PASS is "
        "necessary, not sufficient"
        if g.passed else
        "long-only GP/A does not clear the fund's bar on honest point-in-time "
        "membership; the graveyard verdict is recorded verbatim and the factor "
        "must not proceed toward approval (a failed gate is a deliverable, not "
        "a defect to be tuned away)")
    fold_rets = ", ".join(f"{x.total_return:+.2%}" for x in wf.fold_results)
    yearly = [mc for mc in run.member_counts if mc[0].month == 12]
    xfin = bool(run.excluded_financials)

    lines = [
        f"# THE QUALITY TEST — Novy-Marx GP/A on the point-in-time S&P 500 "
        f"({run.return_convention.split(' ')[0]}"
        f"{', financials excluded' if xfin else ''})",
        "",
        "> ## STRATEGY CANDIDATE #3 THROUGH THE IDENTICAL BAR",
        "> The ONLY difference from the momentum and PEAD runs is the signal: "
        "names are",
        "> ranked by gross profitability — trailing-four-quarter gross profit "
        "over most-",
        "> recent total assets (Novy-Marx 2013) — not 12-1 momentum or SUE. "
        "Universe,",
        "> delisting-aware engine, top-decile equal-weight monthly "
        "construction, monkey",
        "> null, deflated Sharpe, purged walk-forward and the binding beat-SPY "
        "bar are",
        "> REUSED BY IMPORT from xsmom_pit_run and the committed gauntlet.",
        "",
        "> ## NO LOOK-AHEAD IS STRUCTURAL (signals/quality/v1.py)",
        "> filing_date gates when a quarter's figures are knowable; a filing "
        "is tradable",
        "> only the NEXT session; a quarter's GP/A is knowable only at the "
        "LATEST of its",
        "> four input filings; vendor rows stamped filing_date <= period end "
        "(a probed",
        "> defect) are dropped fail-closed at ingestion and never stored; and "
        "a filing",
        "> dated after the decision session is physically excluded from the "
        "ranking. A",
        "> future quarter's numbers can be flipped wildly and the ranking at t "
        "is",
        "> byte-identical (pinned by test).",
        "",
        f"Pinned spec (textbook, zero search): GP/A = trailing "
        f"{TRAILING_QUARTERS} quarters of grossProfit / most recent "
        f"totalAssets, quarterly statements; all {TRAILING_QUARTERS} quarters "
        f"+ the newest balance sheet required, else ineligible (missing "
        f"grossProfit is NEVER derived from revenue minus a cost line); "
        f"consecutive quarters enforced structurally (period-end span <= "
        f"{CONSECUTIVE_SPAN_DAYS} days); staleness {STALENESS_SESSIONS} "
        f"sessions (an annual cycle without a fresh filing — structural, the "
        f"paper uses annual data). Winner portfolio is the top decile "
        f"(max(10, n_eligible // {DECILE})), equal weight, monthly.",
        "",
        f"- Evaluation window STARTS {WINDOW_START} (membership-reliability "
        f"bound); costs {COSTS.commission_bps}+{COSTS.slippage_bps} bps/side; "
        f"null {paths}-path monkey MC drawing from the SAME GP/A-eligible set; "
        f"purged walk-forward k={K_FOLDS}, horizon={HORIZON}, embargo={EMBARGO}; "
        "one registered trial per family; deflated Sharpe at the true "
        "LINEAGE count (ADR-0016).",
        f"- Binding benchmark: SPY "
        f"{'total return' if 'total' in run.return_convention else 'buy-and-hold'} "
        "over the same window (ADR-0009); SPY carries no membership row and can "
        "never be ranked. Equal-weight-all-eligible shown, NOT binding.",
        "- FINANCIALS: Novy-Marx 2013 EXCLUDES financial firms (structurally "
        "low GP/A). "
        + (f"THIS run applies that exclusion explicitly "
           f"({len(run.excluded_financials)} names, family `{run.family}`); "
           f"{run.members_null_sector} members carry no sector classification "
           "and stay in (the approximation is visible, never silent)."
           if xfin else
           "This run does NOT exclude them — the full point-in-time universe "
           "is ranked and financials simply score what they score. A "
           "financials-excluded variant exists behind --exclude-financials as "
           "a SECOND registered trial; the orchestrator decides whether to "
           "spend it."),
        "",
        "## Data quality and honesty — fundamentals coverage",
        "",
        f"- Panel members with a usable price series: {cov.symbols_in_panel}",
        f"- Members carrying >= 1 stored anchorable quarter: "
        f"{cov.symbols_with_fundamentals} ({cov.total_quarters} quarters on "
        f"record; {cov.delisted_with_fundamentals} of them delisted names — "
        "survivorship-free)",
        f"- Members that ever produce an in-window signal event: "
        f"{cov.symbols_with_signal}",
        "- KNOWN COVERAGE COST (fail-closed, not tuned): quarters the vendor "
        "stamps with filing_date <= fiscal period end (a physically impossible "
        "filing day; e.g. ALL of AVGO 2012-2017) are dropped at ingestion — "
        "trusting them would inject weeks of look-ahead. Affected names go "
        "signal-less until four consecutive anchorable quarters accumulate; "
        "the per-run drop counts are on the ingestion audit event "
        "(market.quarterly_fundamentals_ingest.completed).",
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
        f"(lineage '{run.lineage}', {g.n_trials} trials; must be >= {DSR_MIN})",
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
            f"### Exhibit: verdict vs endpoint "
            f"({run.return_convention.split(' ')[0]})",
            "",
            f"The identical run re-judged at the final date and each of the "
            f"prior {ENDPOINT_MONTHS} month-ends (exact truncation of the stored "
            "curves). A ROBUST edge beats SPY at most endpoints; an edge that "
            "beats SPY at only the terminal endpoints is time-concentrated and "
            "fragile — read the count below against that standard, not as a "
            "guarantee.",
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
        f"| GP/A quality, PIT S&P 500 winner decile{' (xfin)' if xfin else ''} "
        f"| {r.total_return:+.2%} "
        f"| {g.spy_bh_return:+.2%} | {g.ew_return:+.2%} | {r.sharpe:.2f} "
        f"| {r.max_drawdown:.2%} | {r.avg_turnover:.2%} | {r.n_rebalances} "
        f"| {g.null_p_value:.3f} | {g.dsr:.3f} ({g.n_trials}) "
        f"| {wf.positive_folds}/{len(wf.fold_results)} | **{verdict}** |",
        "",
        f"Trial registry: **{run.trials_before_total} → {run.trials_after_total}** "
        f"(one `{run.family}` trial; lineage '{run.lineage}' count now "
        f"{run.n_trials}).",
        "",
        *_annual_distribution_lines(run),
        "## Approval status",
        "",
        "**None sought here — by design.** This is a VALIDATION run on a "
        "membership-gated universe of validation-only instruments "
        "(is_active=FALSE); it settles whether long-only gross profitability "
        "is a real, orthogonal alpha source on honest membership. It does not "
        "itself qualify any strategy for the approval workflow. Gates were not "
        "modified; no strategy row is touched.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(
        description="Novy-Marx GP/A quality on the point-in-time S&P 500")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--total-return", action="store_true")
    p.add_argument("--exclude-financials", action="store_true",
                   help="the paper's financials exclusion — registers a SECOND "
                        "trial family ('-xfin'); OFF by default, orchestrator "
                        "decision")
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
        run = run_quality_pit(s, audit, paths=a.paths,
                              total_return=a.total_return,
                              exclude_financials=a.exclude_financials)
        endpoints = (verdict_vs_endpoint(run) if a.total_return else None)
        report = render_quality_report(run, paths=a.paths, endpoints=endpoints)
        if a.total_return:
            kill = run_quality_pit(s, audit, paths=a.paths, total_return=True,
                                   window_start=KILL_START,
                                   exclude_financials=a.exclude_financials)
            report += ("\n\n---\n\n## Pre-committed KILL-ONLY trial "
                       f"(start {KILL_START}, family {kill.family})\n\n"
                       + render_quality_report(kill, paths=a.paths))
        default = TR_REPORT if a.total_return else PRICE_REPORT
        out = a.report or default
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report)

    print(f"quality-gpa: gate={'PASS' if run.gate.passed else 'FAIL'} "
          f"return={run.run.result.total_return:+.2%} "
          f"spy={run.gate.spy_bh_return:+.2%} p={run.gate.null_p_value:.3f} "
          f"dsr={run.gate.dsr:.3f} wf={run.wf.positive_folds}/"
          f"{len(run.wf.fold_results)} (reasons: "
          f"{list(run.gate.reasons) or 'none'})")
    print(f"report written: {out}")


if __name__ == "__main__":
    main()
