"""FX sandbox gauntlet (ADR-0008 §5): the benchmark is ZERO.

There is nothing to hold in FX, so no buy-and-hold leg exists; a candidate
must beat doing nothing, after honest costs, through the full discipline:

1. EVERY run registers a trial in quant.trial_registry — the SAME registry as
   the equity fund (ADR-0008 §3: trials are trials), family ``fxlab-<name>``.
   Deflated Sharpe uses the TRUE count of ALL ``fxlab-`` trials, not just the
   candidate's own family: the sandbox's candidates are sibling attempts at
   one target (EUR/USD daily), so counting only a candidate's own family
   would understate the search breadth. All trials in a batch are registered
   BEFORE any gate is computed, so every candidate is deflated by the same,
   order-independent count. Strictly more conservative than the equity
   per-family convention — gates may only ever be biased AGAINST a candidate.
2. Null model: random-entry LONG/SHORT — the candidate's own position blocks
   (run-length segments), order-shuffled per seeded path, evaluated through
   the SAME engine and cost constants. Exposure is matched exactly (same
   multiset of session positions); turnover is matched from above (adjacent
   equal blocks can only merge, which only CHEAPENS the null — conservative).
3. Thresholds (null p, deflated Sharpe) are read off the equity gate's
   signature (dcp/backtest/validation.py) and NEVER restated — if the house
   gate tightens, fxlab tightens with it.
4. Purged walk-forward with real_run's K_FOLDS/HORIZON/EMBARGO, folds built
   by the shared purged_folds; candidates are unfitted (textbook constants),
   so train days are unused — same treatment as momentum v1 in real_run.
   Clearing means the same majority rule the equity approval gate applies
   (dcp/backtest/approval.py): positive folds >= k//2 + 1.

PASS requires ALL of: total_return > 0 after costs (benchmark zero), null p
<= p_max, DSR >= dsr_min, walk-forward majority. Most candidates are EXPECTED
to fail (ADR-0008 Consequences); verdicts are recorded verbatim, and no gate
here may ever be weakened to change one.

NO PROFIT TARGET EXISTS ANYWHERE IN THIS MODULE (ADR-0008 §7). If a candidate
passes, its earnings profile is a DERIVED output reported afterward —
whatever the numbers are.

Usage: python -m atlas.fxlab.gauntlet [--paths 1000] [--seed 7]
"""
from __future__ import annotations

import argparse
import inspect
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, pstdev

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.real_run import EMBARGO, HORIZON, K_FOLDS
from atlas.dcp.backtest.registry import lineage_count, register_trial
from atlas.dcp.backtest.validation import deflated_sharpe, null_model_gate
from atlas.dcp.backtest.walkforward import leakage_free, purged_folds
from atlas.fxlab.candidates import CANDIDATES, WARMUP
from atlas.fxlab.engine import (SPREAD_PER_SIDE, SWAP_PER_NIGHT, FxBar, FxResult,
                                FxStrategy, run_fx_backtest, run_fx_positions)

ROOT = Path(__file__).resolve().parents[2]
PAIR = "EURUSD"
NULL_PATHS = 1000

# Thresholds are the equity gate's own defaults, read off its signature —
# imported discipline, never restated (ADR-0008 §3).
_GATE_PARAMS = inspect.signature(null_model_gate).parameters
P_MAX = float(_GATE_PARAMS["p_max"].default)
DSR_MIN = float(_GATE_PARAMS["dsr_min"].default)


def position_segments(positions: list[int]) -> list[tuple[int, int]]:
    """Run-length segments [(position, length), ...] of a position sequence."""
    segs: list[tuple[int, int]] = []
    for p in positions:
        if segs and segs[-1][0] == p:
            segs[-1] = (p, segs[-1][1] + 1)
        else:
            segs.append((p, 1))
    return segs


def block_path(segs: list[tuple[int, int]], rng: random.Random) -> list[int]:
    """One null path: the candidate's own blocks in a random order — exposure
    matched exactly, turnover matched from above (adjacent equal blocks can
    only merge, which only cheapens the null)."""
    shuffled = segs[:]
    rng.shuffle(shuffled)
    return [p for p, length in shuffled for _ in range(length)]


def null_total_returns(bars: list[FxBar], positions: list[int], start_i: int,
                       *, paths: int, seed: int) -> list[float]:
    """Random long/short null: the candidate's own blocks, order-shuffled,
    through run_fx_positions — identical engine, spread and swap."""
    segs = position_segments(positions)
    rng = random.Random(seed)
    return [run_fx_positions(bars, block_path(segs, rng), start_i).total_return
            for _ in range(paths)]


@dataclass(frozen=True)
class FxWalkForward:
    fold_results: list[FxResult]
    positive_folds: int
    mean_return: float
    worst_fold_return: float


def fx_walk_forward(bars: list[FxBar], strategy: FxStrategy, *,
                    warmup: int) -> FxWalkForward:
    results: list[FxResult] = []
    for fold in purged_folds(len(bars), k=K_FOLDS, horizon=HORIZON,
                             embargo=EMBARGO, warmup=warmup):
        assert leakage_free(fold, horizon=HORIZON, embargo=EMBARGO)
        # candidates are unfitted textbook rules: train days are unused
        # (same treatment as momentum v1 in dcp/backtest/real_run.py)
        results.append(run_fx_backtest(bars, strategy,
                                       start_i=fold.test_start, end_i=fold.test_end))
    rets = [r.total_return for r in results]
    return FxWalkForward(fold_results=results,
                         positive_folds=sum(1 for x in rets if x > 0),
                         mean_return=fmean(rets), worst_fold_return=min(rets))


def wf_majority_ok(positive: int, total: int) -> bool:
    """The equity approval gate's majority rule (dcp/backtest/approval.py)."""
    return positive >= total // 2 + 1


@dataclass(frozen=True)
class FxVerdict:
    name: str
    result: FxResult
    null_p: float
    dsr: float
    n_trials: int
    wf: FxWalkForward
    trial_id: str
    passed: bool
    reasons: list[str]


def evaluate_candidate(*, name: str, bars: list[FxBar], result: FxResult,
                       n_trials: int, trial_id: str, wf: FxWalkForward,
                       paths: int, seed: int) -> FxVerdict:
    nulls = null_total_returns(bars, result.positions, WARMUP,
                               paths=paths, seed=seed)
    p = sum(1 for x in nulls if x >= result.total_return) / len(nulls)
    dsr = deflated_sharpe(result.sharpe, len(bars) - WARMUP, n_trials)
    reasons: list[str] = []
    if result.total_return <= 0.0:
        reasons.append(f"does not beat doing nothing: {result.total_return:.2%} <= 0% "
                       "after costs (ADR-0008 §5: the benchmark is zero)")
    if p > P_MAX:
        reasons.append(f"null-model: p={p:.3f} > {P_MAX} "
                       "(random long/short blocks do as well)")
    if dsr < DSR_MIN:
        reasons.append(f"deflated Sharpe {dsr:.2f} < {DSR_MIN} at n_trials={n_trials}")
    if not wf_majority_ok(wf.positive_folds, len(wf.fold_results)):
        reasons.append(f"walk-forward: only {wf.positive_folds}/{len(wf.fold_results)} "
                       "folds positive")
    return FxVerdict(name=name, result=result, null_p=p, dsr=dsr, n_trials=n_trials,
                     wf=wf, trial_id=trial_id, passed=not reasons, reasons=reasons)


def load_bars(session: Session, pair: str = PAIR) -> list[FxBar]:
    rows = session.execute(text(
        "SELECT bar_date, open, high, low, close FROM fxlab.bars_daily "
        "WHERE pair = :p ORDER BY bar_date"), {"p": pair}).all()
    return [FxBar(bar_date=r.bar_date, open=float(r.open), high=float(r.high),
                  low=float(r.low), close=float(r.close)) for r in rows]


# ADR-0016: every fxlab-* family belongs to the sandbox's own research line.
FXLAB_LINEAGE = "fxlab"


def fxlab_trial_count(session: Session) -> int:
    """TRUE deflation count: every trial the sandbox has ever registered —
    the 'fxlab' lineage (ADR-0016; legacy rows backfilled by migration 0032,
    so this equals the historical LIKE 'fxlab-%' count)."""
    return lineage_count(session, FXLAB_LINEAGE)


def run_gauntlet(session: Session, audit: PostgresAuditLog, *,
                 paths: int = NULL_PATHS, seed: int = 7) -> list[FxVerdict]:
    bars = load_bars(session)
    if len(bars) < WARMUP + 60:
        raise RuntimeError(f"only {len(bars)} EURUSD bars — not enough to evaluate; "
                           "run python -m atlas.fxlab.ingest first")
    n = len(bars)
    window = f"{bars[0].bar_date}..{bars[-1].bar_date}"

    results = {name: run_fx_backtest(bars, strat, WARMUP, n)
               for name, (strat, _) in CANDIDATES.items()}

    # Register ALL trials before computing any gate: every candidate in the
    # batch is deflated by the same true count (order-independent).
    trial_ids: dict[str, str] = {}
    for name, (_, spec) in CANDIDATES.items():
        r = results[name]
        trial_ids[name] = register_trial(
            session, family=f"fxlab-{name}", lineage=FXLAB_LINEAGE,
            spec={**spec, "data": "EODHD real", "window": window, "warmup": WARMUP,
                  "spread_per_side": SPREAD_PER_SIDE, "swap_per_night": SWAP_PER_NIGHT},
            metrics={"total_return": r.total_return, "sharpe": r.sharpe,
                     "max_drawdown": r.max_drawdown, "n_trades": float(r.n_trades),
                     "exposure_long": r.exposure_long,
                     "exposure_short": r.exposure_short})
    n_trials = fxlab_trial_count(session)

    verdicts: list[FxVerdict] = []
    for name, (strat, _) in CANDIDATES.items():
        wf = fx_walk_forward(bars, strat, warmup=WARMUP)
        v = evaluate_candidate(name=name, bars=bars, result=results[name],
                               n_trials=n_trials, trial_id=trial_ids[name],
                               wf=wf, paths=paths, seed=seed)
        audit.append(
            event_type="quant.backtest.completed", entity_type="strategy",
            entity_id=f"fxlab/{name}", actor_type="dcp", actor_id="fxlab.gauntlet",
            payload={"pair": PAIR, "trial_id": v.trial_id, "n_trials": n_trials,
                     "window": window, "bars": n, "benchmark": "zero (ADR-0008 §5)",
                     "gate_passed": v.passed, "gate_reasons": list(v.reasons),
                     "null_p": v.null_p, "dsr": v.dsr,
                     "wf_positive_folds": v.wf.positive_folds})
        verdicts.append(v)
    return verdicts


def render_report(verdicts: list[FxVerdict], *, window: str, n_bars: int,
                  paths: int, seed: int, trials_before: int,
                  trials_after: int) -> str:
    lines = [
        "# fxlab gauntlet — EUR/USD daily, three textbook candidates (2026-07)",
        "",
        "> ## ADR-0008: the benchmark is ZERO and no profit target exists",
        "> There is nothing to hold in FX: a candidate must beat doing",
        "> nothing, after honest costs, through the full gauntlet. **No profit",
        "> target exists anywhere in the sandbox** — the Principal's original",
        '> "self-learn until it generates A$50/day" framing was REFUSED and is',
        "> recorded in ADR-0008: a learning loop with a profit quota as its",
        "> stopping rule converges on memorized noise. If something passes,",
        "> its earnings profile (expectancy, Sharpe, drawdown, P&L dispersion)",
        "> is a DERIVED output reported afterward — whatever the numbers are.",
        "> **The expected outcome of this report is failure**; verdicts are",
        "> recorded verbatim.",
        "",
        f"Window: {window} ({n_bars} vendor bars, EODHD; volume and weekend stubs",
        f"discarded as vendor artifacts). Warmup {WARMUP} bars (longest candidate lookback).",
        "",
        "- Engine: daily long/short in {-1, 0, +1}, decided on close of t,",
        "  executed at open of t+1; final open position force-liquidated at the",
        "  last close (atlas/fxlab/engine.py)",
        "- Honest costs (ADR-0008 §4, conservative placeholders, ADR-0003 Tier-1",
        f"  recalibratable): spread {SPREAD_PER_SIDE:.5f} per position-change leg",
        f"  (a round trip pays {2 * SPREAD_PER_SIDE:.5f} ~ 1.6 pips), swap",
        f"  {SWAP_PER_NIGHT:.5f} per night held, either direction",
        f"- Null model: {paths} seeded paths (seed {seed}) of the candidate's own",
        "  position blocks order-shuffled — matched exposure, turnover matched",
        "  from above — through the SAME engine and costs",
        "- Thresholds read from dcp/backtest/validation.py, never restated:",
        f"  null p <= {P_MAX}, deflated Sharpe >= {DSR_MIN} at the true count of",
        "  ALL fxlab- trials in quant.trial_registry (same registry as the fund)",
        f"- Purged walk-forward: k={K_FOLDS}, horizon={HORIZON}, embargo={EMBARGO}",
        "  (imported from dcp/backtest/real_run.py); clearing = the approval",
        "  gate's majority rule (positive folds >= k//2 + 1)",
        "",
        f"Trial registry: **{trials_before} fxlab trials before this run -> "
        f"{trials_after} after**; deflated Sharpe below uses n_trials="
        f"{verdicts[0].n_trials if verdicts else trials_after}.",
        "",
    ]
    for v in verdicts:
        r, wf = v.result, v.wf
        fold_rets = ", ".join(f"{x.total_return:+.2%}" for x in wf.fold_results)
        lines += [
            f"## {v.name}",
            "",
            f"Full-window result (after {WARMUP}-bar warmup): "
            f"return {r.total_return:+.2%} vs benchmark 0.00%, "
            f"Sharpe {r.sharpe:.2f}, max drawdown {r.max_drawdown:.2%}, "
            f"{r.n_trades} trades; exposure long {r.exposure_long:.0%} / "
            f"short {r.exposure_short:.0%} / flat {r.exposure_flat:.0%}",
            "",
            f"### Gate verdict: **{'PASS' if v.passed else 'FAIL'}**",
            "",
            f"- strategy return after costs: {r.total_return:+.2%} (must be > 0%)",
            f"- null-model p-value: {v.null_p:.3f} (must be <= {P_MAX})",
            f"- deflated Sharpe: {v.dsr:.3f} at n_trials={v.n_trials} "
            f"(lineage '{FXLAB_LINEAGE}', {v.n_trials} trials; "
            f"must be >= {DSR_MIN})",
            f"- trial registry id: `{v.trial_id}`",
            "",
        ]
        if v.reasons:
            lines.append("Verbatim gate reasons:")
            lines += [f"- {reason}" for reason in v.reasons]
            lines.append("")
        lines += [
            f"### Walk-forward: {wf.positive_folds}/{len(wf.fold_results)} "
            "folds positive",
            "",
            f"- fold returns: {fold_rets}",
            f"- mean fold return {wf.mean_return:+.2%}, "
            f"worst fold {wf.worst_fold_return:+.2%}",
            "",
        ]
    passed = [v for v in verdicts if v.passed]
    lines += [
        "## Verdict table",
        "",
        "| candidate | return | Sharpe | max DD | trades | long/short/flat "
        "| null p | DSR (n_trials) | WF folds + | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in verdicts:
        r = v.result
        lines.append(
            f"| {v.name} | {r.total_return:+.2%} | {r.sharpe:.2f} "
            f"| {r.max_drawdown:.2%} | {r.n_trades} "
            f"| {r.exposure_long:.0%}/{r.exposure_short:.0%}/{r.exposure_flat:.0%} "
            f"| {v.null_p:.3f} | {v.dsr:.3f} ({v.n_trials}) "
            f"| {v.wf.positive_folds}/{len(v.wf.fold_results)} "
            f"| **{'PASS' if v.passed else 'FAIL'}** |")
    lines += [
        "",
        "## Earnings profile (ADR-0008 §7)",
        "",
    ]
    if passed:
        lines += [
            "Derived AFTER the verdicts, for passing candidates only — reported",
            "whatever the numbers are, never targeted:",
            "",
        ]
        for v in passed:
            r = v.result
            n_days = len(r.equity) - 1
            per_trade = r.total_return / r.n_trades if r.n_trades else 0.0
            daily = [r.equity[j] / r.equity[j - 1] - 1 for j in range(1, len(r.equity))]
            lines += [
                f"- {v.name}: expectancy/trade {per_trade:+.4%}, annualized Sharpe "
                f"{r.sharpe:.2f}, max drawdown {r.max_drawdown:.2%}, daily P&L "
                f"dispersion (stdev) {pstdev(daily):.4%} over {n_days} sessions",
            ]
        lines.append("")
    else:
        lines += [
            "**REFUSED — nothing passed.** Earnings profiles are DERIVED outputs",
            "of a candidate that has survived the gauntlet (ADR-0008 §7); no",
            "candidate did, so there is no earnings profile to report and none",
            "will be projected, extrapolated or targeted. Profit is a result to",
            "be discovered, never an input parameter.",
            "",
        ]
    lines += [
        "## Status",
        "",
        "Research-only, forever, under ADR-0008: no live trading, no paper",
        "ledger shared with the equity book, no path to the risk engine, bridge,",
        "desk or approval queue. Promotion out of the sandbox would require a",
        "new, separate signed ADR. Gates were not modified for this run; FAIL",
        "verdicts above are deliverables, recorded verbatim.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(description="fxlab EUR/USD candidate gauntlet")
    p.add_argument("--paths", type=int, default=NULL_PATHS)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--report", type=Path,
                   default=ROOT / "docs" / "reports" / "fxlab-eurusd-2026-07.md")
    a = p.parse_args()

    with session_scope() as s:
        last_bar = s.execute(text(
            "SELECT max(bar_date) FROM fxlab.bars_daily WHERE pair = :p"),
            {"p": PAIR}).scalar()
        if last_bar is None:
            raise SystemExit("no EURUSD bars — run python -m atlas.fxlab.ingest first")
        # deterministic clock: derived from the data, not the wall (house pattern)
        clock = FrozenClock(datetime(last_bar.year, last_bar.month, last_bar.day,
                                     22, 0, tzinfo=UTC))
        audit = PostgresAuditLog(s, clock)
        trials_before = fxlab_trial_count(s)
        bars = load_bars(s)
        verdicts = run_gauntlet(s, audit, paths=a.paths, seed=a.seed)
        trials_after = fxlab_trial_count(s)

    report = render_report(verdicts, window=f"{bars[0].bar_date}..{bars[-1].bar_date}",
                           n_bars=len(bars), paths=a.paths, seed=a.seed,
                           trials_before=trials_before, trials_after=trials_after)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(report)
    for v in verdicts:
        print(f"{v.name}: gate={'PASS' if v.passed else 'FAIL'} "
              f"wf={v.wf.positive_folds}/{len(v.wf.fold_results)} "
              f"(reasons: {list(v.reasons) or 'none'})")
    print(f"fxlab trials: {trials_before} -> {trials_after}")
    print(f"report written: {a.report}")


if __name__ == "__main__":
    main()
