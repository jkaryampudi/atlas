"""RECIPE GAUNTLET RUNNER (Research Factory phase 1): a validated RecipeSpec
-> trial registered BEFORE anything runs -> the FULL portfolio gauntlet the
committed runners already passed, reused by import — never restated.

WHAT RUNS (all imported: xsmom_pit_run / impl_variant_run /
portfolio_validation / real_run):
  * load_pit_panel — point-in-time S&P 500 membership (fail-closed interval
    rule, delisted names included, SPY outside the ranked universe), TOTAL
    RETURN convention on every series (ADR-0009's binding benchmark);
  * the rank feature MATERIALIZED into the point-in-time feature store
    (factory/features catalog; dataset_version pins the data vintage) and
    read back through the store — the runner ranks on STORED values, so the
    store itself is on the hook for byte-identity with production math;
  * run_pit_backtest — the delisting-aware engine, monthly rebalance,
    next-open execution, the committed 10 bps/side CostModel;
  * 1000-path seeded monkey null drawing the SAME top_n construction from
    the SAME point-in-time eligible set (ADR-0002 #2);
  * deflated Sharpe at the TRUE lineage trial count (ADR-0002 #1 /
    ADR-0016), thresholds imported from portfolio_validation;
  * purged+embargoed walk-forward (real_run constants) with SPY through the
    identical fold machinery;
  * SPY buy-and-hold total return — the BINDING bar (ADR-0009);
  * the verdict-vs-endpoint exhibit (exact truncation of stored curves);
  * the PRE-COMMITTED kill trial (spec.kill_start): second registration,
    demote-only.

REGISTRATION-BEFORE-RUN (count honesty): each trial is registered — with the
canonical spec, the spec_hash (the row's spec_hash column carries the exact
hash the report and audit event print, so the row traces to its spec), the
rationale as `hypothesis` and the pinned dataset_version — BEFORE its
backtest executes, with EMPTY metrics, and the registration COMMITS IN ITS
OWN TRANSACTION (a second session on the run session's engine) before any
gauntlet math runs. WHY A SEPARATE COMMITTED TRANSACTION: the CLI wraps the
whole gauntlet pair in one session_scope() transaction that rolls back on
any exception, so a registration left on the run session would be theater —
every crashed attempt would silently un-count, and a kill-leg crash would
erase the completed main trial (the exact result-conditioned discard
ADR-0002 #1 exists to prevent). The finished run enriches the SAME row's
metrics in place, likewise committed per leg on completion — the main
trial's registration AND metrics are durable before the kill leg starts
(never a second row, never a delete — quant.trial_registry is not the audit
chain; append-only lives in audit.decision_events). DOCUMENTED RESIDUAL: the
quant.backtest.completed audit event stays on the run transaction (it must
chain onto the caller's audit state), so a crashed gauntlet leaves a durable
stub trial row with no audit event — the count is honest; the crashed
attempt's only durable trace is the registry row itself.

RANKING BASIS, stated honestly: stored features carry the PRODUCTION signal
math — split-adjusted PRICE closes (features/momentum.py == the live
generator's formation return, equivalence-pinned) — while accounting and
benchmark are total-return. The impl-variant runner ranks on TR panel closes
instead; the two coincide exactly on names that paid no dividends (the pin
fixture ASSERTS that precondition; it is load-bearing), and on real
dividend-paying data they legitimately DIVERGE: the recipe then runs a
DIFFERENT trial from xsmom-impl500-tr — deliberately, because the price
basis is what the LIVE generator actually trades — and its verdict is its
own, never a reproduction of the impl-variant verdict. That divergence is a
basis difference, not a store bug: do NOT "fix" either side toward the other
(rebasing the store on TR closes would break its genuine golden pin against
the production ranker, test_feature_equivalence_pg). The store-lies rule is
scoped to VALUE mismatches only: a missing store value for an eligible name
is a RuntimeError, never a silent skip — if the store cannot serve the
production math it claims to carry, fix the store side, never the
production side.

Do NOT tune anything to pass — a failed gate is a valid, reportable result.

Usage:
  python -m atlas.dcp.factory.recipe_run --spec recipe.json [--paths 1000]
      [--seed 7] [--window-end 2026-07-15] [--report PATH]
  python -m atlas.dcp.factory.recipe_run --spec recipe.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import random
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.backtest.impl_variant_run import _endpoint_verdicts, truncate_panel
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
from atlas.dcp.backtest.xsmom_run import total_trial_count
from atlas.dcp.backtest.xsmom_pit_run import (
    BENCHMARK,
    ENDPOINT_MONTHS,
    TR_CONVENTION,
    EndpointVerdict,
    PitBacktest,
    PitUniverse,
    load_pit_panel,
    pit_eligible,
    pit_equal_weight,
    pit_walk_forward,
    run_pit_backtest,
)
from atlas.dcp.factory.features import FEATURE_LINEAGE, get_rank_feature
from atlas.dcp.factory.spec import RecipeSpec, RecipeSpecError, spec_from_mapping
from atlas.dcp.features.store import MaterializeReport, feature_panel, materialize
from atlas.dcp.market_data.index_membership import (
    INDEX_CODE,
    WINDOW_START,
    MembershipRow,
    is_member_on,
)
from atlas.dcp.signals.xsmom.v1 import SEASONING

ROOT = Path(__file__).resolve().parents[3]

RANK_BASIS = ("stored feature values: split-adjusted PRICE closes — the "
              "production signal generator's basis (features/momentum.py, "
              "equivalence-pinned); accounting and benchmark are "
              "total-return per ADR-0009")


# ---------------------------------------------------------------------------
# Feature materialization for the run (store-side; dataset_version pinned)
# ---------------------------------------------------------------------------

def rebalance_superset(dates: list[date]) -> list[date]:
    """Every session that can be a rebalance decision in ANY window over this
    panel: month_end_indices judges month boundaries on the panel's own
    session sequence, so month_end_indices(dates, a, b) ⊆
    {t : dates[t].month != dates[t+1].month} for every (a, b) — the full
    run, the kill run and every purged walk-forward fold all rebalance
    inside this set. Materializing exactly here keeps the store dense where
    decisions happen and nowhere else."""
    return [dates[t] for t in range(len(dates) - 1)
            if dates[t].month != dates[t + 1].month]


def materialize_rank_feature(session: Session, spec: RecipeSpec, *,
                             clock: Clock, symbols: list[str],
                             sessions: list[date]) -> MaterializeReport:
    """Register (idempotently, pin-checked) and materialize the spec's rank
    feature over the panel's rebalance sessions. FAIL-LOUD, unlike the
    fail-soft backfill CLI: a symbol the panel accepted but the store cannot
    compute would silently shrink the ranked universe — refused."""
    feature = get_rank_feature(spec.rank_feature)
    report = materialize(session, feature, clock=clock, symbols=symbols,
                         sessions=sessions)
    if report.failed:
        raise RuntimeError(
            f"feature {feature.name} failed for {list(report.failed)} — the "
            f"store must serve every panel symbol: {list(report.failures)}")
    return report


# ---------------------------------------------------------------------------
# The spec-driven strategy, its fair monkey and the EW benchmark
# ---------------------------------------------------------------------------

def recipe_strategy(members: Mapping[str, MembershipRow],
                    values: Mapping[str, Mapping[date, float]],
                    top_n: int) -> PortfolioStrategy:
    """Rank the point-in-time eligible set by the STORED feature value at the
    decision session, descending with the deterministic (-value, symbol)
    tie-break — the production ranking order — and hold the top_n equal
    weight (fewer eligible -> hold them all, never pad)."""
    def strat(view: PanelView) -> dict[str, float]:
        today = view.today
        ranked: list[tuple[float, str]] = []
        for s in pit_eligible(view, members):
            got = values.get(s, {}).get(today)
            if got is None:
                raise RuntimeError(
                    f"feature value missing for eligible {s} at {today} — "
                    "the store failed to serve the production math (fix the "
                    "store side, never the production side)")
            ranked.append((got, s))
        ranked.sort(key=lambda rs: (-rs[0], rs[1]))
        top = ranked[:top_n]
        if not top:
            return {}
        w = 1.0 / len(top)
        return {s: w for _, s in top}
    return strat


def recipe_null_results(panel: PricePanel,
                        members: Mapping[str, MembershipRow], *, top_n: int,
                        paths: int, seed: int,
                        start: date) -> list[PortfolioResult]:
    """The fair monkey null (ADR-0002 #2), construction-identical to
    impl_variant_run.impl_null_results for a single full-budget sleeve: at
    each rebalance every monkey draws min(top_n, |eligible|) names uniformly
    without replacement from the IDENTICAL sorted point-in-time eligible set
    the strategy ranks, equal weight, through the IDENTICAL delisting-aware
    engine and costs. One rng drives all paths sequentially (the
    validation.py convention); full results kept so the endpoint exhibit
    truncates stored curves exactly."""
    rng = random.Random(seed)
    cache: dict[int, tuple[str, ...]] = {}

    def monkey(view: PanelView) -> dict[str, float]:
        base = cache.get(view.t)
        if base is None:
            base = tuple(sorted(pit_eligible(view, members)))
            cache[view.t] = base
        if not base:
            return {}
        pick = rng.sample(list(base), min(top_n, len(base)))
        w = 1.0 / len(pick)
        return {s: w for s in pick}

    return [run_pit_backtest(panel, monkey, COSTS, start=start).result
            for _ in range(paths)]


# ---------------------------------------------------------------------------
# Orchestration: one gauntlet per (spec, window), verdicts verbatim
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecipeRun:
    spec: RecipeSpec
    family: str
    start: date
    dataset_version: str
    materialization: MaterializeReport
    universe: PitUniverse
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
    counts: tuple[tuple[date, int, int], ...]   # (rebalance, members, eligible)
    panel_first: date
    panel_last: date


def _registry_session(session: Session) -> Session:
    """A SECOND session on the run session's own engine, for registry writes
    that must SURVIVE the run transaction (module docstring: count honesty
    is durability, not intent — the CLI's session_scope rolls the whole run
    back on any exception). Both production (session_scope) and the test
    fixtures bind their sessions to an Engine, so this opens an independent
    connection whose commits are untouched by a later rollback of the run
    transaction; the run session reads the committed rows straight back
    (READ COMMITTED, Postgres' default)."""
    bind = session.get_bind()
    if not isinstance(bind, Engine):  # pragma: no cover — structural guard
        raise RuntimeError(
            "registry durability needs a session bound to an Engine — a "
            "session bound to a shared Connection would re-tie the "
            "registration to the run transaction it must outlive")
    return Session(bind=bind)


def _record_final_metrics(session: Session, trial_id: str,
                          metrics: dict[str, float]) -> None:
    """Enrich the PRE-REGISTERED trial row with the finished run's metrics,
    COMMITTED in its own transaction (module docstring): the leg's completed
    result is durable the moment it exists, so a kill-leg crash can no
    longer erase the completed main trial's metrics. The COUNT is the
    invariant (ADR-0002 #1): it was fixed at registration, before any result
    existed; this UPDATE touches only the metrics of that same row — never a
    second row, never a delete. quant.trial_registry is not the audit chain
    (append-only lives in audit.decision_events)."""
    upd = _registry_session(session)
    try:
        upd.execute(text(
            "UPDATE quant.trial_registry SET metrics = CAST(:m AS jsonb) "
            "WHERE id = CAST(:id AS uuid)"),
            {"m": json.dumps(metrics), "id": trial_id})
        upd.commit()
    except BaseException:
        upd.rollback()
        raise
    finally:
        upd.close()


class DuplicateFamilyError(RuntimeError):
    """The family already has a registered trial and rerun was not requested —
    the accidental double-burn (two surfaces racing one hypothesis) is refused
    at the write chokepoint itself, never merely at an advisory pre-check."""


def run_recipe(session: Session, audit: PostgresAuditLog, spec: RecipeSpec, *,
               clock: Clock, paths: int = 1000, seed: int = 7,
               window_start: date | None = None,
               window_end: date | None = None,
               rerun: bool = False) -> RecipeRun:
    """One full gauntlet for `spec`. window_start may ONLY be the spec's own
    pre-committed kill_start (bounded grammar: no ad-hoc windows).

    rerun=False (the default) enforces ONE NAME, ONE EXPERIMENT at the
    registration chokepoint: inside the registration transaction, a
    pg_advisory_xact_lock on the family serializes concurrent registration
    attempts across ALL processes (console, CLI, anything), and a count taken
    under that lock refuses a family that already holds a registered trial —
    the race that could burn two trial pairs for one hypothesis is impossible,
    not merely unlikely. A DELIBERATE repeat (e.g. exact reproduction with
    --window-end; --rerun on the CLI) sets rerun=True and registers honestly
    as a new counted trial in the same family."""
    if window_start is not None and window_start != spec.kill_start:
        raise ValueError(
            f"window_start {window_start} is not the spec's pre-committed "
            f"kill_start {spec.kill_start} — ad-hoc windows are outside the "
            "v1 grammar")
    committed = COSTS.commission_bps + COSTS.slippage_bps
    if float(spec.cost_bps_per_side) != committed:  # pragma: no cover — pin
        raise RuntimeError(
            f"spec costs {spec.cost_bps_per_side} bps/side != committed "
            f"CostModel {committed} — the fixed-cost pin is broken")
    family = spec.family() if window_start is None else spec.kill_family()

    universe = load_pit_panel(session, window_end=window_end,
                              total_return=True)
    panel = universe.panel
    if window_end is not None:
        panel, _ = truncate_panel(panel, window_end)  # benchmark must survive
    members = {s: r for s, r in universe.members.items() if s in panel.closes}

    mat = materialize_rank_feature(
        session, spec, clock=clock, symbols=sorted(members),
        sessions=rebalance_superset(panel.dates))
    feature = get_rank_feature(spec.rank_feature)
    values = feature_panel(session, feature, sorted(members),
                           start=panel.dates[0], end=panel.dates[-1],
                           dataset_version=mat.dataset_version)

    eval_start = WINDOW_START if window_start is None else window_start
    start_i = bisect_left(panel.dates, eval_start)
    if start_i >= len(panel.dates):
        raise RuntimeError(f"panel ends before the evaluation start {eval_start}")
    if start_i < SEASONING:
        raise RuntimeError(f"only {start_i} sessions precede {eval_start} — "
                           f"the first rebalance needs {SEASONING} sessions")
    start = panel.dates[start_i]

    # REGISTER BEFORE RUNNING (module docstring): the count is inflated now,
    # while no result exists; metrics land on this same row afterwards. The
    # registration commits ON ITS OWN SESSION so it survives a mid-gauntlet
    # crash and the run transaction's rollback — durably registered, not
    # merely inserted (the CLI's session_scope would otherwise unwind it).
    trials_before_total = total_trial_count(session)
    reg_spec: dict[str, object] = {
        **spec.canonical(),
        "family": family,
        "spec_hash": spec.spec_hash(),
        "universe_detail": f"point-in-time {INDEX_CODE} membership "
                           "(validation.index_membership, fail-closed "
                           "interval rule, delisted names included)",
        "return_convention": TR_CONVENTION,
        "ranking_basis": RANK_BASIS,
        "window_start": str(WINDOW_START),
        "evaluation_start": str(eval_start),
        "window": f"{panel.dates[0]}..{panel.dates[-1]}",
        "start": str(start),
        "delisting_rule": "liquidate at final available close, per-side "
                          "cost, proceeds in cash until next rebalance",
        "data": "EODHD real",
        "registered": "pre-run, committed durably before the gauntlet "
                      "(count fixed before any result existed)",
    }
    # Lineage is taken from the CATALOG BINDING, not the spec object —
    # spec validation already forced them equal, but registering from the
    # binding closes even forged-object paths (object.__new__ bypassing
    # __post_init__) at the single write chokepoint (re-attack 2026-07-18).
    bound_lineage = FEATURE_LINEAGE[spec.rank_feature]
    if spec.lineage != bound_lineage:
        raise RecipeSpecError(
            f"spec lineage {spec.lineage!r} does not match the catalog "
            f"binding {bound_lineage!r} for {spec.rank_feature!r} — "
            "refusing to register (forged spec object?)")
    reg = _registry_session(session)
    try:
        # THE DOUBLE-BURN BACKSTOP (chassis review 2026-07, residual closed):
        # a transaction-scoped advisory lock on the family serializes every
        # concurrent registration attempt across all processes; the count
        # taken UNDER the lock is authoritative. Two surfaces racing the same
        # fresh name can no longer both register — the loser waits here, then
        # sees the winner's committed row and refuses. Held only for this
        # short registration transaction, released at commit/rollback.
        reg.execute(text(
            "SELECT pg_advisory_xact_lock(hashtextextended(:f, 0))"),
            {"f": family})
        prior = reg.execute(text(
            "SELECT count(*) FROM quant.trial_registry "
            "WHERE strategy_family = :f"), {"f": family}).scalar() or 0
        if prior and not rerun:
            raise DuplicateFamilyError(
                f"family {family!r} already holds {prior} registered "
                f"trial(s) — one name, one experiment. THIS attempt registers "
                f"nothing further; any legs this gauntlet already completed "
                f"remain honestly counted. A deliberate repeat must say so "
                f"(rerun=True / --rerun) and burns a new counted trial.")
        trial_id = register_trial(
            reg, family=family, lineage=bound_lineage, spec=reg_spec,
            metrics={}, hypothesis=spec.rationale,
            dataset_version=mat.dataset_version,
            spec_hash=spec.spec_hash())
        reg.commit()               # durable BEFORE any gauntlet math runs
    except BaseException:
        reg.rollback()
        raise
    finally:
        reg.close()
    n_trials = lineage_count(session, spec.lineage)
    trials_after_total = total_trial_count(session)

    strategy = recipe_strategy(members, values, spec.top_n)
    pit = run_pit_backtest(panel, strategy, COSTS, start=start)
    result = pit.result

    counts: list[tuple[date, int, int]] = []
    for t in month_end_indices(panel.dates, start_i, len(panel.dates)):
        day = panel.dates[t]
        counts.append((day,
                       sum(1 for r in universe.window_rows
                           if is_member_on(r, day)),
                       len(pit_eligible(PanelView(panel, t), members))))

    nulls = recipe_null_results(panel, members, top_n=spec.top_n,
                                paths=paths, seed=seed, start=start)
    spy = run_pit_backtest(panel, buy_and_hold_strategy(BENCHMARK), COSTS,
                           start=start).result
    ew = run_pit_backtest(panel, pit_equal_weight(members), COSTS,
                          start=start).result
    gate = portfolio_gate(result=result,
                          null_returns=[r.total_return for r in nulls],
                          spy=spy, ew=ew, n_trials=n_trials)
    endpoints = _endpoint_verdicts(result, spy, nulls, n_trials)
    del nulls  # curves served the exhibit; free the memory
    wf = pit_walk_forward(panel, strategy, k=K_FOLDS, horizon=HORIZON,
                          embargo=EMBARGO, warmup=start_i, costs=COSTS)
    wf_spy = pit_walk_forward(panel, buy_and_hold_strategy(BENCHMARK),
                              k=K_FOLDS, horizon=HORIZON, embargo=EMBARGO,
                              warmup=start_i, costs=COSTS)

    _record_final_metrics(session, trial_id, {
        "total_return": result.total_return, "sharpe": result.sharpe,
        "max_drawdown": result.max_drawdown,
        "avg_turnover": result.avg_turnover,
        "n_rebalances": float(result.n_rebalances)})

    audit.append(
        event_type="quant.backtest.completed", entity_type="strategy",
        entity_id=f"{family}/portfolio", actor_type="dcp",
        actor_id="recipe_run",
        payload={"spec_hash": spec.spec_hash(), "family": family,
                 "rank_feature": spec.rank_feature, "top_n": spec.top_n,
                 "lineage": spec.lineage,
                 "dataset_version": mat.dataset_version,
                 "return_convention": TR_CONVENTION,
                 "ranking_basis": RANK_BASIS,
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
                 "endpoints_beat_spy": sum(1 for e in endpoints
                                           if e.beats_spy),
                 "endpoints_pass": sum(1 for e in endpoints if e.passed),
                 "endpoints_total": len(endpoints)})
    return RecipeRun(
        spec=spec, family=family, start=start,
        dataset_version=mat.dataset_version, materialization=mat,
        universe=universe, run=pit, spy=spy, ew=ew, gate=gate, wf=wf,
        wf_spy=wf_spy, endpoints=endpoints, trial_id=trial_id,
        n_trials=n_trials, trials_before_total=trials_before_total,
        trials_after_total=trials_after_total, counts=tuple(counts),
        panel_first=panel.dates[0], panel_last=panel.dates[-1])


def run_recipe_gauntlet(session: Session, audit: PostgresAuditLog,
                        spec: RecipeSpec, *, clock: Clock, paths: int = 1000,
                        seed: int = 7, window_end: date | None = None,
                        rerun: bool = False) -> tuple[RecipeRun, RecipeRun]:
    """The full pre-committed pair: the main trial, then the kill trial
    (spec.kill_start; demote-only). Each registers durably (its own
    committed transaction) BEFORE it runs, and the main trial's metrics are
    committed before the kill leg starts — a kill-leg crash cannot erase the
    completed main trial (module docstring). Both legs carry the double-burn
    backstop (run_recipe docstring); rerun=True is the explicit, counted
    repeat for both."""
    if not rerun:
        # EARLY check of BOTH families before any leg runs: a gauntlet that
        # would be refused at the kill chokepoint AFTER a fully-executed main
        # leg (a half-run pair) is refused HERE instead, burning nothing.
        # Advisory only — the locked per-leg chokepoint stays authoritative;
        # the residual window (a concurrent rerun=True overtaking mid-flight)
        # still refuses at the kill chokepoint with the main leg's burn
        # standing honestly counted (run_recipe docstring).
        reg = _registry_session(session)
        try:
            for fam in (spec.family(), spec.kill_family()):
                prior = reg.execute(text(
                    "SELECT count(*) FROM quant.trial_registry "
                    "WHERE strategy_family = :f"), {"f": fam}).scalar() or 0
                if prior:
                    raise DuplicateFamilyError(
                        f"family {fam!r} already holds {prior} registered "
                        f"trial(s) — one name, one experiment; refused before "
                        f"any leg ran (nothing burned). A deliberate repeat "
                        f"must say rerun=True / --rerun.")
        finally:
            reg.close()
    main = run_recipe(session, audit, spec, clock=clock, paths=paths,
                      seed=seed, window_end=window_end, rerun=rerun)
    kill = run_recipe(session, audit, spec, clock=clock, paths=paths,
                      seed=seed, window_start=spec.kill_start,
                      window_end=window_end, rerun=rerun)
    return main, kill


# ---------------------------------------------------------------------------
# Report (the honest style: verdicts verbatim, n_trials basis named)
# ---------------------------------------------------------------------------

def _run_lines(run: RecipeRun, title: str) -> list[str]:
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
        f"- equal-weight all-eligible TR (informational, NOT binding): "
        f"{g.ew_return:+.2%}",
        f"- null-model p-value: {g.null_p_value:.3f} (must be <= {P_MAX}) — "
        f"monkeys draw {run.spec.top_n} names from the identical eligible "
        "set with the identical construction",
        f"- deflated Sharpe: {g.dsr:.3f} at n_trials={g.n_trials} "
        f"(lineage '{run.spec.lineage}', {g.n_trials} registered trials; "
        f"must be >= {DSR_MIN})",
        f"- trial registry id: `{run.trial_id}` (registered and COMMITTED "
        "before the run; metrics enriched on the same row after)",
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
        f"(final date rolled back to each of the prior {ENDPOINT_MONTHS} "
        "month-ends; exact truncation of the stored strategy/SPY/null curves)",
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


def render_recipe_report(main: RecipeRun, kill: RecipeRun, *,
                         paths: int) -> str:
    spec = main.spec
    tr = main.universe.tr
    assert tr is not None
    lines = [
        f"# RECIPE GAUNTLET — `{main.family}` "
        f"({spec.rank_feature}, top-{spec.top_n}, {spec.rebalance}, "
        f"{spec.universe})",
        "",
        "> ## WHAT THIS IS",
        "> A spec-driven run of the committed portfolio gauntlet (Research "
        "Factory v1).",
        "> Everything is imported from the validated runners — engine, "
        "eligibility, null",
        "> model, thresholds, walk-forward, delisting rule — and the ranking "
        "values come",
        "> from the point-in-time FEATURE STORE, whose equivalence to the "
        "production",
        "> signal math is golden-pinned. Verdicts land verbatim, pass or "
        "fail.",
        "",
        "## The spec (frozen; registered verbatim with both trials)",
        "",
        f"- spec_hash: `{spec.spec_hash()}`",
        f"- name: `{spec.name}`; rank_feature: `{spec.rank_feature}`; "
        f"direction: {spec.direction}; top_n: {spec.top_n}; "
        f"rebalance: {spec.rebalance}; universe: {spec.universe}",
        f"- costs: {spec.cost_bps_per_side} bps/side, FIXED (the committed "
        "CostModel — never a free parameter)",
        f"- lineage: `{spec.lineage}` (ADR-0016 — the deflation count basis)",
        f"- rationale (registered as the trial hypothesis, pre-run): "
        f"{spec.rationale}",
        f"- pre-committed kill start: {spec.kill_start} (demote-only)",
        f"- dataset_version: `{main.dataset_version}` (the feature store's "
        "input-vintage pin)",
        f"- ranking basis: {RANK_BASIS}",
        f"- return convention: {TR_CONVENTION}",
        "",
        "## Panel and coverage (loader inherited unchanged)",
        "",
        f"- Panel {main.panel_first} → {main.panel_last}; members with "
        f"usable series: {len(main.universe.members)} "
        f"({main.universe.included_delisted} delisted); missing series: "
        f"{len(main.universe.missing_series)}; SPY carries "
        f"{tr.spy_dividends} reinvested distributions (asserted non-zero)",
        f"- Feature materialization: {main.materialization.sessions} "
        f"rebalance sessions, {main.materialization.inserted} values "
        f"inserted, {main.materialization.existing} already present, "
        "0 failures (fail-loud)",
        f"- Null model: {paths}-path seeded monkey MC (ADR-0002 #2); "
        f"walk-forward k={K_FOLDS}, horizon={HORIZON}, embargo={EMBARGO} "
        "(real_run constants, ADR-0002 #3)",
        "",
        *_run_lines(main, f"Trial 1 — `{main.family}`: the recipe on its "
                          "full window"),
        "# Pre-committed kill trial (demote-only)",
        "",
        f"Identical recipe, evaluation start {spec.kill_start} — "
        "pre-committed in the spec BEFORE any result existed. A PASS here "
        "validates nothing by itself; a FAIL is a strike.",
        "",
        *_run_lines(kill, f"Trial 2 — `{kill.family}`: the kill window"),
        "## Summary",
        "",
        "| trial | window | strategy TR | SPY TR | margin | null p | "
        "DSR (n) | WF+ | endpoints beat/pass/total | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in (main, kill):
        g = r.gate
        lines.append(
            f"| `{r.family}` | {r.start} → {r.panel_last} "
            f"| {g.strategy_return:+.2%} | {g.spy_bh_return:+.2%} "
            f"| {g.strategy_return - g.spy_bh_return:+.2%} "
            f"| {g.null_p_value:.3f} | {g.dsr:.3f} ({g.n_trials}) "
            f"| {r.wf.positive_folds}/{len(r.wf.fold_results)} "
            f"| {sum(1 for e in r.endpoints if e.beats_spy)}/"
            f"{sum(1 for e in r.endpoints if e.passed)}/{len(r.endpoints)} "
            f"| **{'PASS' if g.passed else 'FAIL'}** |")
    lines += [
        "",
        f"Trial registry: **{main.trials_before_total} trials before this "
        f"run → {kill.trials_after_total} after** (two pre-committed "
        f"registrations; lineage '{spec.lineage}' count now "
        f"{kill.n_trials}).",
        "",
        "## Approval status",
        "",
        "**None sought here — by design.** A recipe PASS only means the "
        "recipe may be taken to the separate approval workflow "
        "(dcp/backtest/approval.py) by the Principal; the gates were not "
        "modified and no strategy row is touched.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dry run (validate + plan; NO database, NO registration, NO run)
# ---------------------------------------------------------------------------

def dry_run_plan(spec: RecipeSpec, *, paths: int, seed: int) -> str:
    """The plan a real invocation would execute, from the validated spec
    alone. Touches no database: nothing is registered, nothing runs."""
    feature = get_rank_feature(spec.rank_feature)
    return "\n".join([
        "DRY RUN — recipe validated; plan only (no trial registered, no "
        "backtest executed, no database touched)",
        "",
        f"spec_hash: {spec.spec_hash()}",
        f"recipe: rank by {spec.rank_feature} ({spec.direction}), "
        f"top-{spec.top_n} equal weight, {spec.rebalance} rebalance, "
        f"universe {spec.universe}",
        f"rank feature pin: version {feature.version}, "
        f"code_sha {feature.code_sha()[:12]}…, spec {dict(feature.spec)}",
        f"costs: {spec.cost_bps_per_side} bps/side (fixed)",
        f"lineage: {spec.lineage} (deflated Sharpe counts this line's "
        "registered trials)",
        f"rationale (becomes the registered hypothesis): {spec.rationale}",
        "",
        "would register (BEFORE running, count honesty):",
        f"  1. family `{spec.family()}` — full window from {WINDOW_START}",
        f"  2. family `{spec.kill_family()}` — pre-committed kill window "
        f"from {spec.kill_start} (demote-only)",
        "",
        f"gauntlet (all imported, thresholds unmodified): {paths}-path "
        f"seeded monkey null (seed {seed}, p <= {P_MAX}), deflated Sharpe "
        f">= {DSR_MIN} at the true lineage count, beat SPY buy-and-hold "
        f"TOTAL return (binding, ADR-0009), purged walk-forward k={K_FOLDS} "
        f"horizon={HORIZON} embargo={EMBARGO}, verdict-vs-endpoint exhibit "
        f"({ENDPOINT_MONTHS} month-ends)",
        f"ranking basis: {RANK_BASIS}",
    ])


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Spec-driven recipe gauntlet on the point-in-time "
                    "feature store (Research Factory v1)")
    p.add_argument("--spec", required=True, type=Path,
                   help="path to the recipe JSON (the bounded v1 grammar)")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--window-end", type=date.fromisoformat, default=None,
                   help="cut the panel here for exact reproduction of an "
                        "earlier run")
    p.add_argument("--report", type=Path, default=None,
                   help="report path (default docs/reports/recipe-<name>.md)")
    p.add_argument("--dry-run", action="store_true",
                   help="validate the spec and print the plan; registers "
                        "nothing, runs nothing, touches no database")
    p.add_argument("--rerun", action="store_true",
                   help="deliberately repeat a family that already holds a "
                        "registered trial (e.g. exact reproduction with "
                        "--window-end); burns a NEW counted trial pair — "
                        "without this flag a duplicate family is refused at "
                        "the registration chokepoint")
    return p


def main() -> None:
    a = _build_parser().parse_args()
    spec = spec_from_mapping(json.loads(a.spec.read_text()))
    if a.dry_run:
        print(dry_run_plan(spec, paths=a.paths, seed=a.seed))
        return

    from atlas.core.clock import FrozenClock
    from atlas.core.db import session_scope
    report_path: Path = a.report or (
        ROOT / "docs" / "reports" / f"recipe-{spec.name}.md")
    with session_scope() as s:
        # deterministic clock: derived from the data, not the wall
        last_bar = s.execute(text(
            "SELECT max(bar_date) FROM market.price_bars_daily "
            "WHERE source='EodhdAdapter'")).scalar()
        if last_bar is None:
            raise SystemExit("no real bars stored — run the backfill first")
        clock = FrozenClock(datetime(last_bar.year, last_bar.month,
                                     last_bar.day, 22, 0, tzinfo=UTC))
        audit = PostgresAuditLog(s, clock)
        main_run, kill_run = run_recipe_gauntlet(
            s, audit, spec, clock=clock, paths=a.paths, seed=a.seed,
            window_end=a.window_end, rerun=a.rerun)

    report = render_recipe_report(main_run, kill_run, paths=a.paths)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    for r in (main_run, kill_run):
        g = r.gate
        print(f"{r.family}/portfolio: gate={'PASS' if g.passed else 'FAIL'} "
              f"return={g.strategy_return:+.2%} spy={g.spy_bh_return:+.2%} "
              f"margin={g.strategy_return - g.spy_bh_return:+.2%} "
              f"p={g.null_p_value:.3f} dsr={g.dsr:.3f} "
              f"(n_trials={g.n_trials}, lineage '{r.spec.lineage}') "
              f"wf={r.wf.positive_folds}/{len(r.wf.fold_results)} "
              f"(reasons: {list(g.reasons) or 'none'})")
    print(f"trials: {main_run.trials_before_total} -> "
          f"{kill_run.trials_after_total}")
    print(f"report written: {report_path}")


if __name__ == "__main__":
    main()
