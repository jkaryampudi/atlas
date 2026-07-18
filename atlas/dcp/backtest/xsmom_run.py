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

ADDITIVE symbols mode (survivorship cross-check; defaults unchanged): with
--symbols the panel is built from EXACTLY the named symbols regardless of
is_active (validation-only instruments are inactive by design — see
market_data/validation_universe.py), --top-n sets the proportional winner-
portfolio size (v1's TOP_N=10 of ~110 is the winner decile; a 9-ETF universe
takes the winner third, top 3 of 9 — J&T's construction is fractional, not an
absolute count), --family names the trial family. When the SPY benchmark is
not among the requested symbols it is loaded on a SIDE PANEL with an identical
session axis (fail-loud), so the eligible set the strategy, the monkey null
and the equal-weight benchmark all face is exactly the requested universe.

Usage: python -m atlas.dcp.backtest.xsmom_run [--paths 1000]
       python -m atlas.dcp.backtest.xsmom_run --symbols XLB,... --top-n 3 \
           --family xsmom-etf   (survivorship cross-check)
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.dcp.backtest.portfolio import (
    PanelView,
    PortfolioResult,
    PortfolioStrategy,
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
from atlas.dcp.backtest.registry import lineage_count, register_trial
from atlas.dcp.market_data.calendars import trading_days_between
from atlas.dcp.signals.xsmom.v1 import (
    LOOKBACK,
    SEASONING,
    SKIP,
    SPEC,
    TOP_N,
    eligible_symbols,
    xsmom_v1,
)

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


def load_universe_panel(session: Session, *,
                        symbols: list[str] | None = None) -> UniverseLoad:
    """Aligned open/close matrix over every universe name with vendor bars,
    applying the documented exclusion rules (each exclusion counted).

    Documented resolution (validation instruments): the default panel is the
    TRADABLE universe — `i.is_active` is filtered explicitly, so validation-only
    instruments (is_active=FALSE, e.g. the survivorship-check sector ETFs) can
    never leak into the signed ADR-0007 run. Every tradable instrument is
    active, so the default panel is unchanged by the filter. With ``symbols``
    the panel is EXACTLY the named symbols regardless of is_active (validation
    universes are inactive by design); a requested symbol with no vendor bars
    at all refuses loudly — fail closed, never a silently thinner universe."""
    if symbols is None:
        rows = session.execute(text(
            "SELECT DISTINCT i.symbol, i.market FROM market.instruments i "
            "JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
            "WHERE pb.source = 'EodhdAdapter' AND i.is_active "
            "ORDER BY i.symbol")).all()
    else:
        rows = session.execute(text(
            "SELECT DISTINCT i.symbol, i.market FROM market.instruments i "
            "JOIN market.price_bars_daily pb ON pb.instrument_id = i.id "
            "WHERE pb.source = 'EodhdAdapter' AND i.symbol = ANY(:syms) "
            "ORDER BY i.symbol"), {"syms": symbols}).all()
        absent = sorted(set(symbols) - {r.symbol for r in rows})
        if absent:
            raise RuntimeError(f"requested symbol(s) with no vendor bars: "
                               f"{absent} — run the symbols backfill first")
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


def xsmom_top(n: int) -> PortfolioStrategy:
    """The SAME Jegadeesh-Titman 12-1 recipe as signals.xsmom.v1 — identical
    formation window (LOOKBACK/SKIP imported), identical seasoning/eligibility
    (eligible_symbols imported verbatim, so strategy and monkey null face the
    same universe), identical deterministic tie-break — with the winner-
    portfolio SIZE as the one proportional parameter. v1 pins TOP_N=10 because
    on the ~110-name ADR-0007 universe the top 10 IS the winner decile
    (read-only module); J&T's construction is fractional (the winner decile of
    the ranked universe), not an absolute count, so a 9-ETF validation
    universe takes the proportional winner THIRD: top 3 of 9. Pinned by test:
    xsmom_top(TOP_N) reproduces xsmom_v1 weight-for-weight."""
    if n < 1:
        raise ValueError(f"top_n must be >= 1, got {n}")

    def strat(view: PanelView) -> dict[str, float]:
        t = view.t
        ranked: list[tuple[float, str]] = []
        for s in eligible_symbols(view):
            c_form = view.close(s, t - LOOKBACK)
            c_skip = view.close(s, t - SKIP)
            # contiguity: both exist for any eligible symbol (SEASONING == LOOKBACK)
            assert c_form is not None and c_skip is not None
            ranked.append((c_skip / c_form - 1.0, s))
        ranked.sort(key=lambda rs: (-rs[0], rs[1]))
        top = ranked[:n]
        if not top:
            return {}
        w = 1.0 / len(top)
        return {s: w for _, s in top}

    return strat


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
    family: str = "xsmom"
    top_n: int = TOP_N
    lineage: str = "momentum"


def total_trial_count(session: Session) -> int:
    return int(session.execute(text(
        "SELECT count(*) FROM quant.trial_registry")).scalar() or 0)


def run_xsmom(session: Session, audit: PostgresAuditLog, *,
              paths: int = 1000, seed: int = 7,
              symbols: list[str] | None = None, top_n: int | None = None,
              family: str = "xsmom") -> XsmomRun:
    """Defaults reproduce the ADR-0007 run byte-for-byte. The additive
    symbols mode (survivorship cross-check) changes ONLY: the panel (exactly
    the named symbols), the winner-portfolio size (proportional --top-n; the
    monkey null picks the same n from the same eligible set), the trial
    family, and — when SPY is not among the requested symbols — the benchmark
    source: SPY buy-and-hold runs on a side panel with an identical session
    axis (fail-loud), keeping it out of the strategy's/null's/EW's universe."""
    universe = load_universe_panel(session, symbols=symbols)
    panel = universe.panel
    if len(panel.dates) < SEASONING + 40:
        raise RuntimeError(f"only {len(panel.dates)} sessions — not enough to "
                           "season a single name")
    if BENCHMARK not in universe.included and symbols is None:
        raise RuntimeError(f"benchmark {BENCHMARK} missing from the panel")
    start = panel.dates[SEASONING]
    n_pick = TOP_N if top_n is None else top_n
    strategy = xsmom_v1 if top_n is None else xsmom_top(top_n)
    universe_label = ("ADR-0007 snapshot (seeds/universe.json)" if symbols is None
                      else f"explicit symbols ({len(symbols)}), validation-only "
                           "(is_active=FALSE; survivorship-free by construction)")

    result = run_portfolio_backtest(panel, strategy, COSTS, start=start)

    trials_before_total = total_trial_count(session)
    spec: dict[str, object] = {
        **SPEC, "universe": universe_label,
        "symbols_included": len(universe.included),
        "symbols_excluded": len(universe.excluded),
        "data": "EODHD real",
        "window": f"{panel.dates[0]}..{panel.dates[-1]}",
        "start": str(start), "costs_bps_per_side":
            COSTS.commission_bps + COSTS.slippage_bps}
    if symbols is not None:
        spec.update({"family": family, "top_n": n_pick,
                     "symbols": sorted(symbols),
                     "top_n_provenance": ("proportional winner fraction: v1's "
                                          "10 of ~110 is the winner decile; "
                                          f"{n_pick} of {len(symbols)} keeps "
                                          "the JT fractional construction")})
    # ADR-0016: every xsmom-* family belongs to the momentum LINEAGE — a new
    # family name never resets the deflated-Sharpe penalty.
    lineage = "momentum"
    trial_id = register_trial(
        session, family=family, lineage=lineage, spec=spec,
        metrics={"total_return": result.total_return, "sharpe": result.sharpe,
                 "max_drawdown": result.max_drawdown,
                 "avg_turnover": result.avg_turnover,
                 "n_rebalances": float(result.n_rebalances)})
    n_trials = lineage_count(session, lineage)
    trials_after_total = total_trial_count(session)

    nulls = portfolio_null_distribution(panel, costs=COSTS, start=start,
                                        n_pick=n_pick, paths=paths, seed=seed)
    if BENCHMARK in universe.included:
        spy = run_portfolio_backtest(panel, buy_and_hold_strategy(BENCHMARK),
                                     COSTS, start=start)
    else:
        bench = load_universe_panel(session, symbols=[BENCHMARK])
        if bench.panel.dates != panel.dates:
            raise RuntimeError(
                f"benchmark {BENCHMARK} session axis "
                f"({bench.panel.dates[0]}..{bench.panel.dates[-1]}, "
                f"{len(bench.panel.dates)} sessions) does not match the panel "
                f"({panel.dates[0]}..{panel.dates[-1]}, {len(panel.dates)}) — "
                "benchmark and strategy must share one calendar")
        spy = run_portfolio_backtest(bench.panel, buy_and_hold_strategy(BENCHMARK),
                                     COSTS, start=start)
    ew = run_portfolio_backtest(panel, equal_weight_eligible, COSTS, start=start)
    gate = portfolio_gate(result=result, null_returns=nulls, spy=spy, ew=ew,
                          n_trials=n_trials)
    wf = portfolio_walk_forward(panel, strategy, k=K_FOLDS, horizon=HORIZON,
                                embargo=EMBARGO, warmup=SEASONING, costs=COSTS)

    survivorship_note = (
        "today's S&P 100 snapshot: index-membership survivorship "
        "bias inflates momentum results; DSR does not correct it; "
        "any PASS is pending point-in-time constituent validation"
        if symbols is None else
        "validation universe: fixed-by-construction sector-fund set, no "
        "index-membership deletion — survivorship-free; cross-checks the "
        "conditional ADR-0007 xsmom PASS")
    audit.append(
        event_type="quant.backtest.completed", entity_type="strategy",
        entity_id=f"{family}/portfolio", actor_type="dcp", actor_id="xsmom_run",
        payload={"universe": ("ADR-0007 snapshot" if symbols is None
                              else universe_label),
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
                 "survivorship_caveat": survivorship_note})
    return XsmomRun(universe=universe, start=start, result=result, spy=spy,
                    ew=ew, gate=gate, wf=wf, trial_id=trial_id,
                    n_trials=n_trials, trials_before_total=trials_before_total,
                    trials_after_total=trials_after_total,
                    family=family, top_n=n_pick, lineage=lineage)


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
        "LINEAGE trial count (ADR-0002 #1, lineage-scoped per ADR-0016)",
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
        f"(lineage '{run.lineage}', {g.n_trials} trials; must be ≥ {DSR_MIN})",
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
        f"→ {run.trials_after_total} after** (ONE xsmom trial; lineage "
        f"'{run.lineage}' count now {run.n_trials}).",
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


# ---------------------------------------------------------------------------
# Annual outcome distribution (validation-report addition, Principal request):
# history is not a forecast — these helpers derive the DISPERSION a strategy
# has exhibited, and they are rendered ONLY for a strategy that passed the
# gauntlet (house rule: earnings profiles are derived for validated
# strategies, never for failed ones; profit is a result, not an input).
# ---------------------------------------------------------------------------

BOOT_BLOCK, BOOT_HORIZON, BOOT_DRAWS, BOOT_SEED = 21, 252, 1000, 7
_PCTS: tuple[tuple[str, float], ...] = (("10th", 0.10), ("25th", 0.25),
                                        ("median", 0.50), ("75th", 0.75),
                                        ("90th", 0.90))


@dataclass(frozen=True)
class YearReturn:
    year: int
    ret: float
    partial: bool
    note: str  # "" for a full year; e.g. "partial (through 2026-07-10)"


def calendar_year_returns(result: PortfolioResult) -> list[YearReturn]:
    """Per-calendar-year compounded returns off the daily equity marks. The
    curve is contiguous over exchange sessions, so only the window edges can
    be partial: the first year when the curve starts after that year's first
    XNYS session, the last when it ends before that year's last session."""
    dates, curve = result.dates, result.equity_curve
    out: list[YearReturn] = []
    for year in range(dates[0].year, dates[-1].year + 1):
        idx = [i for i, d in enumerate(dates) if d.year == year]
        base = curve[idx[0] - 1] if idx[0] > 0 else curve[0]
        ret = curve[idx[-1]] / base - 1.0
        sessions = trading_days_between("US", date(year, 1, 1), date(year, 12, 31))
        note = ""
        if idx[0] == 0 and dates[0] != sessions[0]:
            note = f"partial (from {dates[0]})"
        if idx[-1] == len(dates) - 1 and dates[-1] != sessions[-1]:
            note = (note + "; " if note else "") + f"partial (through {dates[-1]})"
        out.append(YearReturn(year=year, ret=ret, partial=bool(note), note=note))
    return out


def daily_returns(result: PortfolioResult) -> list[float]:
    c = result.equity_curve
    return [c[j] / c[j - 1] - 1.0 for j in range(1, len(c))]


def block_bootstrap_annual(rets: list[float], *, block: int = BOOT_BLOCK,
                           horizon: int = BOOT_HORIZON, draws: int = BOOT_DRAWS,
                           seed: int = BOOT_SEED) -> list[float]:
    """Seeded moving-block bootstrap of annual outcomes: each draw
    concatenates uniformly-placed contiguous blocks of `block` daily returns
    to `horizon` sessions and compounds them. The rng stream depends only on
    (seed, len(rets)), so two same-length series — the strategy and SPY over
    one shared window — draw IDENTICAL block positions: paired draws, same
    method for both columns."""
    if len(rets) < block:
        raise ValueError(f"need >= {block} daily returns, got {len(rets)}")
    rng = random.Random(seed)
    n_blocks = -(-horizon // block)  # ceil
    out: list[float] = []
    for _ in range(draws):
        seq: list[float] = []
        for _ in range(n_blocks):
            j = rng.randrange(len(rets) - block + 1)
            seq.extend(rets[j:j + block])
        g = 1.0
        for r in seq[:horizon]:
            g *= 1.0 + r
        out.append(g - 1.0)
    return out


def percentile(xs: list[float], q: float) -> float:
    """Linear-interpolation percentile (numpy default / R type 7), q in [0,1]."""
    if not xs:
        raise ValueError("empty sample")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")
    ys = sorted(xs)
    pos = q * (len(ys) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (pos - lo)


def _annual_distribution_lines(run: XsmomRun) -> list[str]:
    """The 'Annual outcome distribution' section. Rendered with numbers ONLY
    for a strategy that passed the gauntlet; a failed strategy gets the house
    rule stated instead of a distribution."""
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
    strat_years = calendar_year_returns(run.result)
    spy_years = {y.year: y for y in calendar_year_returns(run.spy)}
    if set(spy_years) != {y.year for y in strat_years}:
        raise RuntimeError("strategy and SPY cover different years — the "
                           "shared-window invariant is broken")
    for y in strat_years:
        lines.append(f"| {y.year} | {y.ret:+.2%} | {spy_years[y.year].ret:+.2%} "
                     f"| {y.note} |")
    strat_draws = block_bootstrap_annual(daily_returns(run.result))
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


def render_etf_report(run: XsmomRun, *, paths: int) -> str:
    """Survivorship cross-check report (symbols mode). Kept separate from
    render_report so the ADR-0007 report stays byte-identical; verdict
    verbatim, thresholds imported, nothing restated."""
    panel, g, wf, r = run.universe.panel, run.gate, run.wf, run.result
    n_names = len(run.universe.included)
    verdict = "PASS" if g.passed else "FAIL"
    implication = (
        "the cross-sectional momentum effect is REAL on a survivorship-free "
        "universe; the S&P-100 run's +4,584% magnitude remains inflated by "
        "survivorship and stays conditional pending point-in-time constituents"
        if g.passed else
        "the original S&P-100 PASS was likely a survivorship artifact and "
        "must not proceed toward approval on the strength of that run")
    fold_rets = ", ".join(f"{x.total_return:+.2%}" for x in wf.fold_results)
    decision_grade = (panel.dates[-1] - panel.dates[0]).days >= 3650
    lines = [
        f"# Survivorship cross-check — xsmom recipe (12-1, top {run.top_n} "
        f"of {n_names}) on the Select Sector SPDR universe (2026-07)",
        "",
        "> ## WHY THIS UNIVERSE IS SURVIVORSHIP-FREE",
        "> The nine original Select Sector SPDR ETFs (XLB XLE XLF XLI XLK "
        "XLP XLU XLV",
        "> XLY) have traded continuously since December 1998. Sector funds "
        "are never",
        "> deleted for losing, and the sector set is fixed by construction — "
        "no",
        "> index-membership churn, no winners selected into today's list, "
        "and the set",
        "> was fixed decades before this test (no discretion = no selection "
        "bias).",
        "> Sector/industry momentum precedent: Moskowitz & Grinblatt (1999), "
        "\"Do",
        "> Industries Explain Momentum?\", Journal of Finance 54(4).",
        ">",
        "> **What this cross-check implies for the conditional S&P-100 "
        "result**",
        "> (docs/reports/xsmom-momentum-2026-07.md): a PASS here means the "
        "cross-sectional",
        "> momentum effect is real though the +4,584% S&P-100 magnitude "
        "remains inflated",
        "> by survivorship; a FAIL here means the original PASS was likely a "
        "survivorship",
        "> artifact. Either way the original verdict stays conditional until "
        "point-in-time",
        "> constituents are tested (see the appendix).",
        "",
        *(["> ## DECISION-GRADE WINDOW (ADR-0004 condition satisfied)",
           f"> Full vendor history ({panel.dates[0]} → {panel.dates[-1]}); "
           "the verdict is",
           "> decision-grade FOR THE CROSS-CHECK QUESTION — pass or fail, "
           "recorded verbatim.",
           ""] if decision_grade else
          ["> ## ⚠️ SMALL-SAMPLE WARNING (ADR-0004)",
           "> Short window; verdicts are **not decision-grade**.",
           ""]),
        "Validation-only universe: the nine ETFs are seeded with "
        "**is_active = FALSE** —",
        "outside the tradable universe, the scanner, the desk and gate "
        "coverage (pinned",
        "by test). The signed manifest (seeds/universe.json, ADR-0007) is "
        "untouched.",
        "",
        "Same textbook recipe as the S&P-100 run (Jegadeesh & Titman 1993, "
        "12-1, monthly,",
        f"equal weight, {SEASONING}-session seasoning), zero parameter "
        "sweeps. The ONE",
        f"proportional adaptation: v1's top 10 of ~110 is the winner decile; "
        f"{n_names} sector",
        f"funds take the winner third, top {run.top_n} of {n_names} (JT's "
        "construction is",
        "fractional — the winner decile of the ranked universe — not an "
        "absolute count).",
        f"ONE registered trial (family `{run.family}`). Gate thresholds are "
        "IMPORTED from",
        "the committed validation module — nothing restated, nothing tuned.",
        "",
        f"- Engine: portfolio target-weight, monthly rebalance at month-end "
        f"close, execution at next session's open, costs "
        f"{COSTS.commission_bps}+{COSTS.slippage_bps} bps/side on turnover",
        f"- Null model: {paths}-path monkey MC — at each rebalance, "
        f"{run.top_n} names drawn uniformly from the SAME eligible set, "
        "identical engine/costs (ADR-0002 #2)",
        f"- Walk-forward: purged+embargoed on the daily timeline, k={K_FOLDS}, "
        f"horizon={HORIZON}, embargo={EMBARGO} (constants from real_run), "
        f"warmup={SEASONING} (ADR-0002 #3)",
        "- Registered in quant.trial_registry; deflated Sharpe uses the true "
        "LINEAGE trial count (ADR-0002 #1, lineage-scoped per ADR-0016)",
        "- Benchmark: SPY buy-and-hold on a side panel sharing the identical "
        "session axis (SPY is deliberately NOT in the ranked universe); "
        f"equal-weight all-{n_names} shown per protocol, NOT binding",
        "- Convention note (inherited from the round-2 machinery, applied "
        "identically to strategy, null, and both benchmarks): bars are "
        "split-adjusted PRICE returns — dividends/distributions are not "
        "reinvested on either side of the comparison",
        "",
        "## Universe and data honesty",
        "",
        f"- Panel: {n_names} symbols included, "
        f"{panel.dates[0]} → {panel.dates[-1]} "
        f"({len(panel.dates)} aligned XNYS sessions, split-adjusted)",
        f"- Included: {', '.join(run.universe.included)}",
        f"- Excluded: {len(run.universe.excluded)} symbol(s) — per-instrument "
        "completeness rule (fail closed per series):",
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
        f"- verdict: **{verdict}**",
        f"- implication for the conditional S&P-100 result: {implication}",
        f"- strategy return: {g.strategy_return:+.2%}",
        f"- SPY buy-and-hold (BINDING benchmark — the fund's actual "
        f"alternative): {g.spy_bh_return:+.2%}",
        f"- equal-weight all-{n_names}, monthly (informational, shown per "
        f"protocol, NOT binding): {g.ew_return:+.2%}",
        f"- null-model p-value: {g.null_p_value:.3f} (must be ≤ {P_MAX})",
        f"- deflated Sharpe: {g.dsr:.3f} at n_trials={g.n_trials} "
        f"(lineage '{run.lineage}', {g.n_trials} trials; must be ≥ {DSR_MIN})",
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
        "| strategy | return | SPY B&H | EW all-9 | Sharpe | max DD "
        "| avg turnover | rebalances | null p | DSR (n_trials) | WF folds + "
        "| verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        f"| xsmom recipe, top {run.top_n} of {n_names} | {r.total_return:+.2%} "
        f"| {g.spy_bh_return:+.2%} "
        f"| {g.ew_return:+.2%} | {r.sharpe:.2f} | {r.max_drawdown:.2%} "
        f"| {r.avg_turnover:.2%} | {r.n_rebalances} | {g.null_p_value:.3f} "
        f"| {g.dsr:.3f} ({g.n_trials}) "
        f"| {wf.positive_folds}/{len(wf.fold_results)} | **{verdict}** |",
        "",
        f"Implication: {implication}.",
        "",
        f"Trial registry: **{run.trials_before_total} trials before this run "
        f"→ {run.trials_after_total} after** (ONE {run.family} trial; lineage "
        f"'{run.lineage}' count now {run.n_trials}).",
        "",
        *_annual_distribution_lines(run),
        "## Approval status",
        "",
        "**None sought here — by design.** This is a VALIDATION run on an "
        "untradable (is_active=FALSE) universe: it informs the conditional "
        "ADR-0007 xsmom verdict; it does not itself qualify any strategy for "
        "the approval workflow (dcp/backtest/approval.py). The gates were "
        "not modified; no strategy row is touched.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    from atlas.core.db import session_scope

    p = argparse.ArgumentParser(
        description="xsmom v1 portfolio evaluation over the ADR-0007 universe")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--symbols", default=None,
                   help="validation mode: run over EXACTLY these comma-"
                        "separated symbols regardless of is_active "
                        "(survivorship cross-check)")
    p.add_argument("--top-n", dest="top_n", type=int, default=None,
                   help="winner-portfolio size for --symbols (the proportional "
                        "fraction is a documented decision, never a default)")
    p.add_argument("--family", default=None,
                   help="trial family for --symbols (e.g. xsmom-etf)")
    p.add_argument("--report", type=Path, default=None,
                   help="report path (defaults per mode)")
    a = p.parse_args()

    symbols = ([x.strip() for x in a.symbols.split(",") if x.strip()]
               if a.symbols else None)
    if symbols is None and (a.top_n is not None or a.family is not None):
        p.error("--top-n/--family only apply to --symbols (validation mode)")
    if symbols is not None and (a.top_n is None or a.family is None):
        p.error("--symbols requires explicit --top-n and --family — the "
                "proportional winner fraction and the trial family are "
                "documented decisions, never defaults")
    family = a.family if a.family is not None else "xsmom"
    report_path = a.report or ROOT / "docs" / "reports" / (
        "xsmom-etf-crosscheck-2026-07.md" if symbols else
        "xsmom-momentum-2026-07.md")

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
        run = run_xsmom(s, audit, paths=a.paths, symbols=symbols,
                        top_n=a.top_n, family=family)

    render = render_etf_report if symbols else render_report
    report = render(run, paths=a.paths)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    g = run.gate
    print(f"{run.family}/portfolio: gate={'PASS' if g.passed else 'FAIL'} "
          f"return={g.strategy_return:+.2%} spy={g.spy_bh_return:+.2%} "
          f"ew={g.ew_return:+.2%} p={g.null_p_value:.3f} dsr={g.dsr:.3f} "
          f"wf={run.wf.positive_folds}/{len(run.wf.fold_results)} "
          f"(reasons: {list(g.reasons) or 'none'})")
    print(f"trials: {run.trials_before_total} -> {run.trials_after_total} "
          f"(lineage '{run.lineage}': {run.n_trials})")
    print(f"report written: {report_path}")


if __name__ == "__main__":
    main()
