"""First real-data backtest runner (task 3): momentum v1 over backfilled bars,
through the UNMODIFIED null-model gate and purged walk-forward, every run
registered in quant.trial_registry.

Do NOT tune the strategy to pass — a failed gate is a valid, reportable result.
Per ADR-0004 the one-year window is not decision-grade: no approval is sought
here and the written report must carry the small-sample warning prominently.

Usage: python -m atlas.dcp.backtest.real_run --symbols SPY,AVGO
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.engine import CostModel, OBar, Result, run_backtest
from atlas.dcp.backtest.registry import register_trial, trial_count
from atlas.dcp.backtest.validation import GateReport, null_model_gate
from atlas.dcp.backtest.walkforward import WalkForwardResult, walk_forward
from atlas.dcp.market_data.adjustment import adjust_for_splits
from atlas.dcp.market_data.models import Bar, Split
from atlas.dcp.signals.momentum.v1 import SPEC, momentum_v1

ROOT = Path(__file__).resolve().parents[3]
COSTS = CostModel()
# Evaluation parameters — identical to the synthetic-fixture validation suite
# (tests/unit/test_validation_gates.py, test_walkforward.py). Not tunable here.
WARMUP, K_FOLDS, HORIZON, EMBARGO = 60, 4, 40, 10
AVG_STOP_FRAC, AVG_TARGET_FRAC, TIME_STOP = 0.035, 0.07, 40


@dataclass(frozen=True)
class SymbolRun:
    symbol: str
    n_bars: int
    start: date
    end: date
    result: Result
    gate: GateReport
    wf: WalkForwardResult
    trial_id: str
    n_trials: int


def load_adjusted_obars(session: Session, symbol: str) -> tuple[list[OBar], list[date]]:
    """Real bars (vendor-sourced only), split-adjusted on read."""
    rows = session.execute(text(
        "SELECT pb.bar_date, pb.open, pb.high, pb.low, pb.close, pb.volume "
        "FROM market.price_bars_daily pb "
        "JOIN market.instruments i ON i.id = pb.instrument_id "
        "WHERE i.symbol = :s AND pb.source = 'EodhdAdapter' "
        "ORDER BY pb.bar_date"), {"s": symbol}).all()
    splits = [Split(symbol=symbol, action_date=r.action_date, ratio=Decimal(r.ratio))
              for r in session.execute(text(
                  "SELECT ca.action_date, ca.ratio FROM market.corporate_actions ca "
                  "JOIN market.instruments i ON i.id = ca.instrument_id "
                  "WHERE i.symbol = :s AND ca.action_type = 'split'"), {"s": symbol})]
    bars = [Bar(symbol=symbol, bar_date=r.bar_date, open=Decimal(r.open),
                high=Decimal(r.high), low=Decimal(r.low), close=Decimal(r.close),
                volume=int(r.volume)) for r in rows]
    adjusted = adjust_for_splits(bars, splits)
    obars = [OBar(open=float(b.open), high=float(b.high), low=float(b.low),
                  close=float(b.close), volume=float(b.volume)) for b in adjusted]
    return obars, [b.bar_date for b in adjusted]


def assert_gates_clean(session: Session, market: str, start: date, end: date) -> None:
    """RED blocks downstream (Doc 01): refuse to backtest over untrusted data."""
    red = session.execute(text(
        "SELECT count(*) FROM market.data_quality_gates "
        "WHERE market = :m AND gate_date BETWEEN :a AND :b AND status = 'red'"),
        {"m": market, "a": start, "b": end}).scalar()
    if red:
        raise RuntimeError(f"{red} red gate(s) for {market} in {start}..{end} — "
                           "resolve data quality before backtesting")


def run_symbol(session: Session, audit: PostgresAuditLog, symbol: str, *,
               paths: int = 1000, seed: int = 7) -> SymbolRun:
    obars, dates = load_adjusted_obars(session, symbol)
    if len(obars) < WARMUP + 60:
        raise RuntimeError(f"{symbol}: only {len(obars)} bars — not enough to evaluate")
    assert_gates_clean(session, "US", dates[0], dates[-1])

    n = len(obars)
    result = run_backtest(obars, momentum_v1, COSTS, start_i=WARMUP, end_i=n)

    trial_id = register_trial(
        session, family="momentum",
        spec={**SPEC, "symbol": symbol, "data": "EODHD real",
              "window": f"{dates[0]}..{dates[-1]}", "warmup": WARMUP},
        metrics={"total_return": result.total_return, "sharpe": result.sharpe,
                 "max_drawdown": result.max_drawdown, "hit_rate": result.hit_rate,
                 "n_trades": float(result.n_trades)})
    n_trials = trial_count(session, "momentum")

    gate = null_model_gate(bars=obars, strategy=momentum_v1, result=result,
                           avg_stop_frac=AVG_STOP_FRAC, avg_target_frac=AVG_TARGET_FRAC,
                           time_stop=TIME_STOP, costs=COSTS, start_i=WARMUP, end_i=n,
                           n_trials=n_trials, paths=paths, seed=seed)
    wf = walk_forward(obars, lambda b, t: momentum_v1,
                      k=K_FOLDS, horizon=HORIZON, embargo=EMBARGO, warmup=WARMUP)

    audit.append(event_type="quant.backtest.completed", entity_type="strategy",
                 entity_id=f"momentum/{symbol}", actor_type="dcp", actor_id="real_run",
                 payload={"symbol": symbol, "trial_id": trial_id, "n_trials": n_trials,
                          "window": f"{dates[0]}..{dates[-1]}", "bars": n,
                          "gate_passed": gate.passed, "gate_reasons": list(gate.reasons),
                          "null_p": gate.null_p_value, "dsr": gate.dsr,
                          "wf_positive_folds": wf.positive_folds,
                          "small_sample_warning": "ADR-0004: 1y window, not decision-grade"})
    return SymbolRun(symbol=symbol, n_bars=n, start=dates[0], end=dates[-1],
                     result=result, gate=gate, wf=wf, trial_id=trial_id,
                     n_trials=n_trials)


def render_report(runs: list[SymbolRun], *, paths: int) -> str:
    lines = [
        "# First real-data backtest — momentum v1 (SPY, AVGO)",
        "",
        "> ## ⚠️ SMALL-SAMPLE WARNING (ADR-0004)",
        "> This evaluation runs on **one year** of history (EODHD plan tier).",
        "> Walk-forward folds and deflated-Sharpe estimates on ~250 sessions are",
        "> **indicative only**. Per ADR-0004 condition 1, these verdicts are **not",
        "> decision-grade**: no approval is sought or recorded, and none may be",
        "> until the full-history re-run.",
        "",
        "Gates and evaluation parameters are identical to the committed synthetic-",
        "fixture suite — nothing was tuned for this run (CLAUDE.md: a failing gate",
        "on real data is a valid, reportable result).",
        "",
        f"- Engine: event-driven, next-open entry, costs {COSTS.commission_bps}+"
        f"{COSTS.slippage_bps} bps/side",
        f"- Null model: {paths}-path random-entry MC, identical exits and costs "
        "(ADR-0002 #2)",
        f"- Walk-forward: purged+embargoed, k={K_FOLDS}, horizon={HORIZON}, "
        f"embargo={EMBARGO}, warmup={WARMUP} (ADR-0002 #3)",
        "- Every run registered in quant.trial_registry; deflated Sharpe uses the "
        "true family trial count (ADR-0002 #1)",
        "",
    ]
    for r in runs:
        g, wf = r.gate, r.wf
        fold_rets = ", ".join(f"{x.total_return:+.2%}" for x in wf.fold_results)
        lines += [
            f"## {r.symbol} — {r.start} → {r.end} ({r.n_bars} bars, split-adjusted)",
            "",
            f"Full-window result (after {WARMUP}-bar warmup): "
            f"return {r.result.total_return:+.2%}, Sharpe {r.result.sharpe:.2f}, "
            f"max drawdown {r.result.max_drawdown:.2%}, "
            f"{r.result.n_trades} trades, hit rate {r.result.hit_rate:.0%}",
            "",
            f"### Gate verdict: **{'PASS' if g.passed else 'FAIL'}**",
            "",
            f"- strategy return: {g.strategy_return:+.2%}",
            f"- buy-and-hold return: {g.bh_return:+.2%}",
            f"- null-model p-value: {g.null_p_value:.3f} (must be ≤ 0.05)",
            f"- deflated Sharpe: {g.dsr:.3f} at n_trials={g.n_trials} (must be ≥ 0.90)",
            f"- trial registry id: `{r.trial_id}`",
            "",
        ]
        if g.reasons:
            lines.append("Verbatim gate reasons:")
            lines += [f"- {reason}" for reason in g.reasons]
            lines.append("")
        lines += [
            f"### Walk-forward: {wf.positive_folds}/{len(wf.fold_results)} folds positive",
            "",
            f"- fold returns: {fold_rets}",
            f"- mean return {wf.mean_return:+.2%}, mean Sharpe {wf.mean_sharpe:.2f}, "
            f"worst fold {wf.worst_fold_return:+.2%}",
            "",
        ]
    lines += [
        "## Approval status",
        "",
        "**None sought.** Per ADR-0004, approval decisions on the 1-year window are",
        "not decision-grade; the strategy row remains untouched. Re-run on full",
        "history after the EODHD plan upgrade before any promotion decision.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(description="First real-data momentum v1 evaluation")
    p.add_argument("--symbols", default="SPY,AVGO")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--report", type=Path,
                   default=ROOT / "docs" / "reports" / "first-real-backtest-momentum-v1.md")
    a = p.parse_args()
    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]

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
        runs = [run_symbol(s, audit, sym, paths=a.paths) for sym in symbols]

    report = render_report(runs, paths=a.paths)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(report)
    for r in runs:
        print(f"{r.symbol}: gate={'PASS' if r.gate.passed else 'FAIL'} "
              f"wf={r.wf.positive_folds}/{len(r.wf.fold_results)} "
              f"(reasons: {list(r.gate.reasons) or 'none'})")
    print(f"report written: {a.report}")


if __name__ == "__main__":
    main()
