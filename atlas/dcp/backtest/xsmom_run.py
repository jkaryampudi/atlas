"""Cross-sectional momentum real-data evaluation (strategy R&D round 2):
xsmom v1 (Jegadeesh-Titman 12-1, top 10) over the ADR-0007 universe, through
the portfolio engine and the UNMODIFIED gate thresholds, ONE registered trial
(family 'xsmom'), the verdict recorded verbatim.

SURVIVORSHIP CAVEAT (carried prominently in the report header): the universe
is TODAY'S S&P 100 snapshot (ADR-0007), so a 2010->2026 backtest on it carries
index-membership survivorship bias, which INFLATES cross-sectional momentum
results and which deflated Sharpe does NOT correct. Any PASS is "PASS pending
point-in-time constituent validation". Fetching historical constituents is
deliberately out of scope here.

Data honesty: bars load via the real_run conventions (vendor-sourced,
split-adjusted on read). assert_symbol_data_clean per held/ranked name is
impractical at ~100 symbols, so the documented substitute rule applies:
any symbol whose stored series has missing sessions between ITS inception and
its end is EXCLUDED and counted in the report (fail closed per series);
non-US-calendar names (NDIA on XASX) cannot be aligned to a US session matrix
and are excluded and counted likewise.

Costs and walk-forward constants are imported UNCHANGED from real_run (fixed
policy, not tunable here); the warmup differs by necessity and is documented:
real_run's WARMUP=60 is the indicator warmup for single-series signals, while
xsmom's warmup is its SEASONING=252 sessions (textbook 12 months) — using 60
would only prepend all-cash months.

Do NOT tune the strategy to pass — a failed gate is a valid, reportable
result. No approval is sought here.

Usage: python -m atlas.dcp.backtest.xsmom_run [--paths 1000]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.portfolio import (
    PortfolioResult,
    PricePanel,
    run_portfolio_backtest,
)
from atlas.dcp.backtest.portfolio_validation import (
    DSR_MIN,
    P_MAX,
    PortfolioGateReport,
    PortfolioWalkForwardResult,
    buy_and_hold_strategy,
    equal_weight_eligible,
    portfolio_gate,
    portfolio_null_distribution,
    portfolio_walk_forward,
)
from atlas.dcp.backtest.real_run import (
    COSTS,
    EMBARGO,
    HORIZON,
    K_FOLDS,
    load_adjusted_obars,
)
from atlas.dcp.backtest.registry import register_trial, trial_count
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.signals.xsmom.v1 import SEASONING, SPEC, TOP_N, xsmom_v1

ROOT = Path(__file__).resolve().parents[3]
BENCHMARK = "SPY"


@dataclass(frozen=True)
class Exclusion:
    symbol: str
    reason: str


@dataclass(frozen=True)
class UniverseLoad:
    panel: PricePanel
    included: list[str]
    excluded: list[Exclusion]


def load_universe_panel(session: Session) -> UniverseLoad:
    """Aligned open/close matrix over every universe name with vendor bars,
    applying the documented exclusion rules (each exclusion counted)."""
    rows = session.execute(text(
        "SELECT DISTINCT i.symbol, i.market FROM market.instruments i "
        "JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
        "WHERE pb.source = 'EodhdAdapter' ORDER BY i.symbol")).all()
    excluded: list[Exclusion] = []
    series: dict[str, tuple[list[float], list[float], list[date]]] = {}
    last_session: date | None = None
    for r in rows:
        if r.market != "US":
            excluded.append(Exclusion(r.symbol, f"non-US session calendar "
                                                f"(market={r.market}) — cannot "
                                                "align to a US close matrix"))
            continue
        obars, ds = load_adjusted_obars(session, r.symbol)
        expected = trading_days_between("US", ds[0], ds[-1])
        have = set(ds)
        missing = [d for d in expected if d not in have]
        if missing:
            excluded.append(Exclusion(
                r.symbol, f"{len(missing)} missing session(s) between its "
                          f"inception {ds[0]} and end {ds[-1]} "
                          f"(first: {missing[0]})"))
            continue
        off_calendar = sorted(have - set(expected))
        if off_calendar:
            excluded.append(Exclusion(
                r.symbol, f"{len(off_calendar)} bar(s) on non-session dates "
                          f"(first: {off_calendar[0]})"))
            continue
        series[r.symbol] = ([b.open for b in obars], [b.close for b in obars], ds)
        last_session = ds[-1] if last_session is None else max(last_session, ds[-1])
    if last_session is None:
        raise RuntimeError("no eligible symbols — is the backfill loaded?")
    ends_early = [s for s, (_, _, ds) in series.items() if ds[-1] != last_session]
    for s in ends_early:
        excluded.append(Exclusion(s, f"series ends {series[s][2][-1]}, before the "
                                     f"panel end {last_session} (delisting-shaped)"))
        del series[s]
    first = min(ds[0] for _, _, ds in series.values())
    dates = trading_days_between("US", first, last_session)
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
    return UniverseLoad(panel=PricePanel(dates=dates, opens=opens, closes=closes),
                        included=sorted(series), excluded=excluded)


@dataclass(frozen=True)
class XsmomRun:
    universe: UniverseLoad
    start: date
    result: PortfolioResult
    spy: PortfolioResult
    ew: PortfolioResult
    gate: PortfolioGateReport
    wf: PortfolioWalkForwardResult
    trial_id: str
    n_trials: int
    trials_before_total: int
    trials_after_total: int


def total_trial_count(session: Session) -> int:
    return int(session.execute(text(
        "SELECT count(*) FROM quant.trial_registry")).scalar() or 0)


def run_xsmom(session: Session, audit: PostgresAuditLog, *,
              paths: int = 1000, seed: int = 7) -> XsmomRun:
    universe = load_universe_panel(session)
    panel = universe.panel
    if len(panel.dates) < SEASONING + 40:
        raise RuntimeError(f"only {len(panel.dates)} sessions — not enough to "
                           "season a single name")
    if BENCHMARK not in universe.included:
        raise RuntimeError(f"benchmark {BENCHMARK} missing from the panel")
    start = panel.dates[SEASONING]

    result = run_portfolio_backtest(panel, xsmom_v1, COSTS, start=start)

    trials_before_total = total_trial_count(session)
    trial_id = register_trial(
        session, family="xsmom",
        spec={**SPEC, "universe": "ADR-0007 snapshot (seeds/universe.json)",
              "symbols_included": len(universe.included),
              "symbols_excluded": len(universe.excluded),
              "data": "EODHD real",
              "window": f"{panel.dates[0]}..{panel.dates[-1]}",
              "start": str(start), "costs_bps_per_side":
                  COSTS.commission_bps + COSTS.slippage_bps},
        metrics={"total_return": result.total_return, "sharpe": result.sharpe,
                 "max_drawdown": result.max_drawdown,
                 "avg_turnover": result.avg_turnover,
                 "n_rebalances": float(result.n_rebalances)})
    n_trials = trial_count(session, "xsmom")
    trials_after_total = total_trial_count(session)

    nulls = portfolio_null_distribution(panel, costs=COSTS, start=start,
                                        n_pick=TOP_N, paths=paths, seed=seed)
    spy = run_portfolio_backtest(panel, buy_and_hold_strategy(BENCHMARK),
                                 COSTS, start=start)
    ew = run_portfolio_backtest(panel, equal_weight_eligible, COSTS, start=start)
    gate = portfolio_gate(result=result, null_returns=nulls, spy=spy, ew=ew,
                          n_trials=n_trials)
    wf = portfolio_walk_forward(panel, xsmom_v1, k=K_FOLDS, horizon=HORIZON,
                                embargo=EMBARGO, warmup=SEASONING, costs=COSTS)

    audit.append(
        event_type="quant.backtest.completed", entity_type="strategy",
        entity_id="xsmom/portfolio", actor_type="dcp", actor_id="xsmom_run",
        payload={"universe": "ADR-0007 snapshot",
                 "symbols_included": len(universe.included),
                 "symbols_excluded": len(universe.excluded),
                 "trial_id": trial_id, "n_trials": n_trials,
                 "window": f"{panel.dates[0]}..{panel.dates[-1]}",
                 "start": str(start), "gate_passed": gate.passed,
                 "gate_reasons": list(gate.reasons),
                 "null_p": gate.null_p_value, "dsr": gate.dsr,
                 "spy_bh_return": gate.spy_bh_return,
                 "ew_return": gate.ew_return,
                 "avg_turnover": result.avg_turnover,
                 "n_rebalances": result.n_rebalances,
                 "wf_positive_folds": wf.positive_folds,
                 "survivorship_caveat": (
                     "today's S&P 100 snapshot: index-membership survivorship "
                     "bias inflates momentum results; DSR does not correct it; "
                     "any PASS is pending point-in-time constituent validation")})
    return XsmomRun(universe=universe, start=start, result=result, spy=spy,
                    ew=ew, gate=gate, wf=wf, trial_id=trial_id,
                    n_trials=n_trials, trials_before_total=trials_before_total,
                    trials_after_total=trials_after_total)


def render_report(run: XsmomRun, *, paths: int) -> str:
    panel, g, wf, r = run.universe.panel, run.gate, run.wf, run.result
    verdict = "PASS" if g.passed else "FAIL"
    verdict_line = ("**PASS — pending point-in-time constituent validation** "
                    "(see caveat above)" if g.passed else "**FAIL**")
    fold_rets = ", ".join(f"{x.total_return:+.2%}" for x in wf.fold_results)
    decision_grade = (panel.dates[-1] - panel.dates[0]).days >= 3650
    lines = [
        "# Cross-sectional momentum — xsmom v1 (12-1, top 10) over the "
        "ADR-0007 universe (2026-07)",
        "",
        "> ## ⚠️ SURVIVORSHIP BIAS CAVEAT — read before the verdict",
        "> The universe is **TODAY'S S&P 100 snapshot** (ADR-0007, pinned at",
        "> adoption): every name in it survived and succeeded into 2026. A",
        f"> {panel.dates[0].year}→{panel.dates[-1].year} backtest on it "
        "therefore carries **index-membership",
        "> survivorship bias**, which **INFLATES cross-sectional momentum",
        "> results** (past winners that later collapsed out of the index are",
        "> missing from the loser side of every ranking), and **deflated",
        "> Sharpe does NOT correct for it** — DSR deflates for multiple",
        "> testing, not for a biased universe. Any PASS below is therefore",
        "> **\"PASS pending point-in-time constituent validation\"**. Fetching",
        "> historical constituents is deliberately out of scope for this run.",
        "",
        *(["> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)",
           f"> Full vendor history ({panel.dates[0]} → {panel.dates[-1]}); "
           "verdicts are",
           "> decision-grade subject to the survivorship caveat above — pass "
           "or fail,",
           "> recorded verbatim.",
           ""] if decision_grade else
          ["> ## ⚠️ SMALL-SAMPLE WARNING (ADR-0004)",
           "> Short window; verdicts are **not decision-grade**.",
           ""]),
        "Textbook parameters (Jegadeesh & Titman 1993, cited in the module",
        "docstring), chosen without any parameter search; ONE registered trial",
        "for this run (family `xsmom`). Gate thresholds are IMPORTED from the",
        "committed validation module — nothing restated, nothing tuned",
        "(CLAUDE.md: a failing gate on real data is a valid, reportable "
        "result).",
        "",
        f"- Engine: portfolio target-weight, monthly rebalance at month-end "
        f"close, execution at next session's open, costs "
        f"{COSTS.commission_bps}+{COSTS.slippage_bps} bps/side on turnover",
        f"- Null model: {paths}-path monkey MC — at each rebalance, "
        f"{TOP_N} names drawn uniformly from the SAME eligible set, identical "
        "engine/costs (ADR-0002 #2)",
        f"- Walk-forward: purged+embargoed on the daily timeline, k={K_FOLDS}, "
        f"horizon={HORIZON}, embargo={EMBARGO} (constants from real_run), "
        f"warmup={SEASONING} (xsmom seasoning replaces the single-series "
        "indicator warmup of 60 — documented in xsmom_run) (ADR-0002 #3)",
        "- Registered in quant.trial_registry; deflated Sharpe uses the true "
        "family trial count (ADR-0002 #1)",
        "",
        "## Universe and data honesty",
        "",
        f"- Panel: {len(run.universe.included)} symbols included, "
        f"{panel.dates[0]} → {panel.dates[-1]} "
        f"({len(panel.dates)} aligned XNYS sessions, split-adjusted)",
        f"- Late listings join point-in-time once seasoned "
        f"({SEASONING} prior sessions); they are never backfilled into "
        "earlier rankings",
        f"- Excluded: {len(run.universe.excluded)} symbol(s) — per-instrument "
        "completeness substitute for assert_symbol_data_clean (documented in "
        "xsmom_run):",
        *[f"  - {e.symbol}: {e.reason}" for e in run.universe.excluded],
        "",
        f"## Full-window result (start {run.start}, after "
        f"{SEASONING}-session seasoning)",
        "",
        f"Return {r.total_return:+.2%}, Sharpe {r.sharpe:.2f}, max drawdown "
        f"{r.max_drawdown:.2%}, avg turnover {r.avg_turnover:.2%} per "
        f"rebalance (sum |Δw|, both sides), {r.n_rebalances} rebalances",
        "",
        f"### Gate verdict: **{verdict}**",
        "",
        f"- verdict: {verdict_line}",
        f"- strategy return: {g.strategy_return:+.2%}",
        f"- SPY buy-and-hold (BINDING benchmark — the fund's actual "
        f"alternative): {g.spy_bh_return:+.2%}",
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
        f"| xsmom v1 | {r.total_return:+.2%} | {g.spy_bh_return:+.2%} "
        f"| {g.ew_return:+.2%} | {r.sharpe:.2f} | {r.max_drawdown:.2%} "
        f"| {r.avg_turnover:.2%} | {r.n_rebalances} | {g.null_p_value:.3f} "
        f"| {g.dsr:.3f} ({g.n_trials}) "
        f"| {wf.positive_folds}/{len(wf.fold_results)} | **{verdict}** |",
        "",
        f"Trial registry: **{run.trials_before_total} trials before this run "
        f"→ {run.trials_after_total} after** (ONE xsmom trial; family count "
        f"now {run.n_trials}).",
        "",
        "## Approval status",
        "",
        "**None sought here — by design.** " + (
            "The verdict is PASS pending point-in-time constituent "
            "validation: the survivorship caveat above must be resolved "
            "(historical index membership) before this result may enter the "
            "approval workflow (dcp/backtest/approval.py). The gates were not "
            "modified; the strategy row is untouched."
            if g.passed else
            "The FAIL verdict is recorded verbatim as a deliverable per the "
            "working-style rule. The gates were not modified; the strategy "
            "row is untouched."),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(
        description="xsmom v1 portfolio evaluation over the ADR-0007 universe")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--report", type=Path,
                   default=ROOT / "docs" / "reports" / "xsmom-momentum-2026-07.md")
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
        run = run_xsmom(s, audit, paths=a.paths)

    report = render_report(run, paths=a.paths)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(report)
    g = run.gate
    print(f"xsmom/portfolio: gate={'PASS' if g.passed else 'FAIL'} "
          f"return={g.strategy_return:+.2%} spy={g.spy_bh_return:+.2%} "
          f"ew={g.ew_return:+.2%} p={g.null_p_value:.3f} dsr={g.dsr:.3f} "
          f"wf={run.wf.positive_folds}/{len(run.wf.fold_results)} "
          f"(reasons: {list(g.reasons) or 'none'})")
    print(f"trials: {run.trials_before_total} -> {run.trials_after_total} "
          f"(xsmom family: {run.n_trials})")
    print(f"report written: {a.report}")


if __name__ == "__main__":
    main()
