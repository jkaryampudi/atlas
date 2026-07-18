"""Candidate-strategy real-data evaluation (strategy R&D): trend / meanrev /
breakout v1 over the full backfilled history, through the UNMODIFIED null-model
gate and purged walk-forward, every run registered in quant.trial_registry.

Evaluation policy — WARMUP/K_FOLDS/HORIZON/EMBARGO, the null-model exit
fractions and the cost model — is imported UNCHANGED from real_run (the
momentum harness): it is fixed policy, not tunable here. One registered trial
per (family, symbol); textbook parameters only, NO sweeps, so the family
n_trials counts stay honest for deflated Sharpe.

Do NOT tune a strategy to pass — a failed gate is a valid, reportable result.
No approval is sought here: a PASS only means the candidate may enter the
separate approval workflow (dcp/backtest/approval.py), run by the Principal.

Usage: python -m atlas.dcp.backtest.candidate_run \
           --families trend,meanrev,breakout --symbols SPY,QQQ,MSFT,AVGO
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
from atlas.dcp.backtest.engine import Result, Strategy, run_backtest
from atlas.dcp.backtest.real_run import (
    AVG_STOP_FRAC,
    AVG_TARGET_FRAC,
    COSTS,
    EMBARGO,
    HORIZON,
    K_FOLDS,
    TIME_STOP,
    WARMUP,
    assert_symbol_data_clean,
    load_adjusted_obars,
)
from atlas.dcp.backtest.registry import lineage_count, register_trial, trial_count
from atlas.dcp.backtest.validation import GateReport, null_model_gate
from atlas.dcp.backtest.walkforward import WalkForwardResult, walk_forward
from atlas.dcp.signals.breakout.v1 import SPEC as BREAKOUT_SPEC
from atlas.dcp.signals.breakout.v1 import breakout_v1
from atlas.dcp.signals.meanrev.v1 import SPEC as MEANREV_SPEC
from atlas.dcp.signals.meanrev.v1 import meanrev_v1
from atlas.dcp.signals.trend.v1 import SPEC as TREND_SPEC
from atlas.dcp.signals.trend.v1 import trend_v1

ROOT = Path(__file__).resolve().parents[3]

CANDIDATES: dict[str, tuple[Strategy, dict[str, object]]] = {
    "trend": (trend_v1, TREND_SPEC),
    "meanrev": (meanrev_v1, MEANREV_SPEC),
    "breakout": (breakout_v1, BREAKOUT_SPEC),
}

# ADR-0016: these candidate families ARE lineage roots — any future variant
# (e.g. 'trend-tr', 'breakout-pit') must register lineage=the root here, so
# its deflated Sharpe counts every prior trial in the line.
CANDIDATE_LINEAGES: dict[str, str] = {
    "trend": "trend", "meanrev": "meanrev", "breakout": "breakout"}


@dataclass(frozen=True)
class CandidateRun:
    family: str
    symbol: str
    n_bars: int
    start: date
    end: date
    result: Result
    gate: GateReport
    wf: WalkForwardResult
    trial_id: str
    n_trials: int
    lineage: str


def total_trial_count(session: Session) -> int:
    return int(session.execute(text(
        "SELECT count(*) FROM quant.trial_registry")).scalar() or 0)


def run_candidate(session: Session, audit: PostgresAuditLog, family: str,
                  symbol: str, *, paths: int = 1000, seed: int = 7) -> CandidateRun:
    strategy, spec = CANDIDATES[family]
    obars, dates = load_adjusted_obars(session, symbol)
    if len(obars) < WARMUP + 60:
        raise RuntimeError(f"{symbol}: only {len(obars)} bars — not enough to evaluate")
    assert_symbol_data_clean(session, "US", symbol, dates[0], dates[-1])

    n = len(obars)
    result = run_backtest(obars, strategy, COSTS, start_i=WARMUP, end_i=n)

    lineage = CANDIDATE_LINEAGES[family]
    trial_id = register_trial(
        session, family=family, lineage=lineage,
        spec={**spec, "symbol": symbol, "data": "EODHD real",
              "window": f"{dates[0]}..{dates[-1]}", "warmup": WARMUP},
        metrics={"total_return": result.total_return, "sharpe": result.sharpe,
                 "max_drawdown": result.max_drawdown, "hit_rate": result.hit_rate,
                 "n_trades": float(result.n_trades)})
    n_trials = lineage_count(session, lineage)

    gate = null_model_gate(bars=obars, strategy=strategy, result=result,
                           avg_stop_frac=AVG_STOP_FRAC, avg_target_frac=AVG_TARGET_FRAC,
                           time_stop=TIME_STOP, costs=COSTS, start_i=WARMUP, end_i=n,
                           n_trials=n_trials, paths=paths, seed=seed)
    wf = walk_forward(obars, lambda b, t: strategy,
                      k=K_FOLDS, horizon=HORIZON, embargo=EMBARGO, warmup=WARMUP)

    audit.append(event_type="quant.backtest.completed", entity_type="strategy",
                 entity_id=f"{family}/{symbol}", actor_type="dcp",
                 actor_id="candidate_run",
                 payload={"symbol": symbol, "trial_id": trial_id, "n_trials": n_trials,
                          "window": f"{dates[0]}..{dates[-1]}", "bars": n,
                          "gate_passed": gate.passed, "gate_reasons": list(gate.reasons),
                          "null_p": gate.null_p_value, "dsr": gate.dsr,
                          "wf_positive_folds": wf.positive_folds,
                          "window_grade": ("decision-grade: full-history window "
                                           "per ADR-0004 condition"
                                           if (dates[-1] - dates[0]).days >= 3650
                                           else "ADR-0004: short window, "
                                                "not decision-grade")})
    return CandidateRun(family=family, symbol=symbol, n_bars=n, start=dates[0],
                        end=dates[-1], result=result, gate=gate, wf=wf,
                        trial_id=trial_id, n_trials=n_trials, lineage=lineage)


def render_report(runs: list[CandidateRun], *, paths: int,
                  trials_before: int, trials_after: int,
                  momentum_trials: int) -> str:
    decision_grade = all((r.end - r.start).days >= 3650 for r in runs)
    families = sorted({r.family for r in runs},
                      key=lambda f: list(CANDIDATES).index(f))
    lines = [
        "# Candidate strategies — trend / meanrev / breakout v1 (2026-07)",
        "",
        *(["> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)",
           "> This evaluation runs on the **full vendor history** "
           f"({min(r.start for r in runs)} → {max(r.end for r in runs)}).",
           "> Verdicts below are decision-grade: an approval decision MAY rest on",
           "> them — pass or fail, recorded verbatim."]
          if decision_grade else
          ["> ## ⚠️ SMALL-SAMPLE WARNING (ADR-0004)",
           "> This evaluation includes a **short window**; verdicts are",
           "> **not decision-grade** and no approval may rest on them."]),
        "",
        "Three classic families at textbook parameters — cited in each module",
        "docstring, chosen without any parameter search — evaluated through the",
        "UNMODIFIED gates. Nothing was tuned for this run (CLAUDE.md: a failing",
        "gate on real data is a valid, reportable result). One registered trial",
        "per (family, symbol).",
        "",
        f"- Engine: event-driven, next-open entry, costs {COSTS.commission_bps}+"
        f"{COSTS.slippage_bps} bps/side",
        f"- Null model: {paths}-path random-entry MC, identical exits and costs "
        "(ADR-0002 #2)",
        f"- Walk-forward: purged+embargoed, k={K_FOLDS}, horizon={HORIZON}, "
        f"embargo={EMBARGO}, warmup={WARMUP} (ADR-0002 #3)",
        "- Every run registered in quant.trial_registry; deflated Sharpe uses the "
        "true LINEAGE trial count (ADR-0002 #1, lineage-scoped per ADR-0016)",
        "",
        "## Graveyard context",
        "",
        "momentum v1 (trend_rs_vol) **FAILED the gates on real data** — SPY and "
        "AVGO, on the 1y window (`docs/reports/first-real-backtest-momentum-v1.md`) "
        "and again decision-grade on the full 2010→2026 history "
        "(`docs/reports/decision-grade-momentum-v1.md`); the momentum family "
        f"has {momentum_trials} registered trials. Gates were not touched then "
        "and are not touched now.",
        "",
        f"Trial registry: **{trials_before} trials before this run → "
        f"{trials_after} after** (one per family × symbol below).",
        "",
    ]
    for fam in families:
        fam_runs = [r for r in runs if r.family == fam]
        spec = CANDIDATES[fam][1]
        lines += [f"## Family `{fam}` — {spec['name']} v{spec['version']}", ""]
        for r in fam_runs:
            g, wf = r.gate, r.wf
            fold_rets = ", ".join(f"{x.total_return:+.2%}" for x in wf.fold_results)
            lines += [
                f"### {fam} × {r.symbol} — {r.start} → {r.end} "
                f"({r.n_bars} bars, split-adjusted)",
                "",
                f"Full-window result (after {WARMUP}-bar warmup): "
                f"return {r.result.total_return:+.2%} vs buy-and-hold "
                f"{g.bh_return:+.2%}, Sharpe {r.result.sharpe:.2f}, "
                f"max drawdown {r.result.max_drawdown:.2%}, "
                f"{r.result.n_trades} trades, hit rate {r.result.hit_rate:.0%}",
                "",
                f"#### Gate verdict: **{'PASS' if g.passed else 'FAIL'}**",
                "",
                f"- strategy return: {g.strategy_return:+.2%}",
                f"- buy-and-hold return: {g.bh_return:+.2%}",
                f"- null-model p-value: {g.null_p_value:.3f} (must be ≤ 0.05)",
                f"- deflated Sharpe: {g.dsr:.3f} at n_trials={g.n_trials} "
                f"(lineage '{r.lineage}', {g.n_trials} trials; must be ≥ 0.90)",
                f"- trial registry id: `{r.trial_id}`",
                "",
            ]
            if g.reasons:
                lines.append("Verbatim gate reasons:")
                lines += [f"- {reason}" for reason in g.reasons]
                lines.append("")
            lines += [
                f"#### Walk-forward: {wf.positive_folds}/{len(wf.fold_results)} "
                "folds positive",
                "",
                f"- fold returns: {fold_rets}",
                f"- mean return {wf.mean_return:+.2%}, mean Sharpe "
                f"{wf.mean_sharpe:.2f}, worst fold {wf.worst_fold_return:+.2%}",
                "",
            ]
    passed = [r for r in runs if r.gate.passed]
    lines += [
        "## Summary",
        "",
        "| family | symbol | return | B&H | Sharpe | max DD | trades | null p "
        "| DSR (n_trials) | WF folds + | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        lines.append(
            f"| {r.family} | {r.symbol} | {r.result.total_return:+.2%} "
            f"| {r.gate.bh_return:+.2%} | {r.result.sharpe:.2f} "
            f"| {r.result.max_drawdown:.2%} | {r.result.n_trades} "
            f"| {r.gate.null_p_value:.3f} | {r.gate.dsr:.3f} ({r.gate.n_trials}) "
            f"| {r.wf.positive_folds}/{len(r.wf.fold_results)} "
            f"| **{'PASS' if r.gate.passed else 'FAIL'}** |")
    lines += [
        "",
        "## Approval status",
        "",
        "**None sought here — by design.** " + (
            ("The following runs passed every gate: "
             + ", ".join(f"{r.family}×{r.symbol}" for r in passed)
             + ". A pass here only makes a candidate ELIGIBLE for the separate "
               "approval workflow (dcp/backtest/approval.py) — artifact-checked, "
               "run by the Principal, never by this harness. The strategy rows "
               "remain untouched.")
            if passed else
            "No candidate passed all gates; per the working-style rule these FAIL "
            "verdicts are recorded verbatim as deliverables. The gates were not "
            "modified, and no strategy row was touched."),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(description="Candidate-strategy real-data evaluation")
    p.add_argument("--families", default="trend,meanrev,breakout")
    p.add_argument("--symbols", default="SPY,QQQ,MSFT,AVGO")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--report", type=Path,
                   default=ROOT / "docs" / "reports" / "candidate-strategies-2026-07.md")
    a = p.parse_args()
    families = [f.strip() for f in a.families.split(",") if f.strip()]
    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]
    unknown = [f for f in families if f not in CANDIDATES]
    if unknown:
        raise SystemExit(f"unknown families: {unknown} (have {list(CANDIDATES)})")

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
        trials_before = total_trial_count(s)
        momentum_trials = trial_count(s, "momentum")
        runs = [run_candidate(s, audit, fam, sym, paths=a.paths)
                for fam in families for sym in symbols]
        trials_after = total_trial_count(s)

    report = render_report(runs, paths=a.paths, trials_before=trials_before,
                           trials_after=trials_after,
                           momentum_trials=momentum_trials)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(report)
    for r in runs:
        print(f"{r.family}/{r.symbol}: gate={'PASS' if r.gate.passed else 'FAIL'} "
              f"wf={r.wf.positive_folds}/{len(r.wf.fold_results)} "
              f"(reasons: {list(r.gate.reasons) or 'none'})")
    print(f"trials: {trials_before} -> {trials_after}")
    print(f"report written: {a.report}")


if __name__ == "__main__":
    main()
