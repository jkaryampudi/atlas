"""One-shot percentile band derivation for an approved strategy — the
approval-contract hardening of board item 7 (ADR-0010/0013: "the
approval-contract build must derive exact percentile bands from the stored
equity curve and may only TIGHTEN these").

WHAT IT DOES, in three deliberate steps:

  1. REGENERATE the strategy's total-return backtest curve in-process, by
     importing the exact building blocks the registered runner used
     (load_pit_panel(total_return=True) + the family's strategy constructor
     + run_pit_backtest + SPY buy-and-hold on the identical panel/window) —
     deterministic: the same stored bars produce byte-identical curves, and
     the curves' sha256 lands in the artifact.
  2. DERIVE the proposed tolerance_bands via band_derivation.py: 1st
     percentile of the trailing-126-session excess distribution, margined
     max-DD floor, CUSUM drift parameters — with the TIGHTEN-ONLY rule
     applied in code against the strategy row's standing bands.
  3. With --apply, UPDATE quant.strategies.tolerance_bands and append a
     quant.strategy.bands_derived audit event carrying the old and new
     bands VERBATIM. The apply path re-checks tighten-only against the
     stored row immediately before writing (defense in depth): if ANY band
     would loosen, it refuses with exit 1 and writes nothing. It also
     refuses when the strategy row is missing — bands are derived only for
     an approved strategy, never invented for one.

WHY NO TRIAL IS REGISTERED HERE (deliberate, argued): invariant 7 exists so
deflated Sharpe uses the TRUE count of hypothesis tests. This tool tests no
hypothesis and reads no gate — it re-materialises the equity curve of a
trial that is ALREADY on the registry (families xsmom-pit-tr / pead-sue-tr)
to measure its percentiles. Padding the registry with byte-identical
regenerations would make the trial count untrue in the other direction.
The regeneration is fully attributable instead: the audit payload and the
derivation artifact record the curve sha256, window and session count.

Run it against the environment the orchestrator points ATLAS_DATABASE_URL
at. Do not run --apply casually: the write is an audit-chained governance
action.

Usage:
    python -m atlas.tools.derive_bands --family xsmom-pit-tr
    python -m atlas.tools.derive_bands --family pead-sue-tr --apply
"""
from __future__ import annotations

import argparse
import json
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock, SystemClock
from atlas.dcp.backtest.band_derivation import (
    DerivedBands,
    derive_proposed_bands,
)
from atlas.dcp.backtest.pead_pit_run import load_pead_signals, pead_pit_strategy
from atlas.dcp.backtest.portfolio_validation import buy_and_hold_strategy
from atlas.dcp.backtest.real_run import COSTS
from atlas.dcp.backtest.xsmom_pit_run import (
    BENCHMARK,
    load_pit_panel,
    run_pit_backtest,
    xsmom_pit_strategy,
)
from atlas.dcp.market_data.index_membership import WINDOW_START
from atlas.dcp.signals.xsmom.v1 import SEASONING
from atlas.dcp.trading.bands import DD_BAND_KEY, EXCESS_BAND_KEY

BANDS_DERIVED_EVENT = "quant.strategy.bands_derived"


@dataclass(frozen=True)
class RegeneratedCurves:
    dates: list[date]
    strategy: list[float]
    spy: list[float]
    note: str


def _tr_curves(session: Session, family: str,
               build_strategy: Callable[..., Any]) -> RegeneratedCurves:
    """The shared curve path of both TR runners, verbatim: TR panel at load
    time, evaluation start at the first session >= WINDOW_START, strategy and
    SPY B&H through the identical delisting-aware engine and costs."""
    universe = load_pit_panel(session, total_return=True)
    panel, members = universe.panel, universe.members
    start_i = bisect_left(panel.dates, WINDOW_START)
    if start_i >= len(panel.dates):
        raise RuntimeError(f"panel ends before the evaluation start "
                           f"{WINDOW_START}")
    if start_i < SEASONING:
        raise RuntimeError(f"only {start_i} sessions precede {WINDOW_START} — "
                           "backfill the formation history first")
    start = panel.dates[start_i]
    strat = build_strategy(session, panel, members)
    result = run_pit_backtest(panel, strat, COSTS, start=start).result
    spy = run_pit_backtest(panel, buy_and_hold_strategy(BENCHMARK), COSTS,
                           start=start).result
    if result.dates != spy.dates:
        raise RuntimeError("strategy and SPY curves cover different sessions "
                           "— the shared-window invariant is broken")
    return RegeneratedCurves(
        dates=list(result.dates), strategy=list(result.equity_curve),
        spy=list(spy.equity_curve),
        note=(f"regenerated in-process: {family} total-return curve + SPY TR "
              f"B&H (load_pit_panel total_return=True, run_pit_backtest, "
              f"costs {COSTS.commission_bps}+{COSTS.slippage_bps} bps/side, "
              f"start {start}); curve of the registered {family} trial — no "
              "new trial registered (no hypothesis tested, no gate read; see "
              "module docstring)"))


def _xsmom_curves(session: Session) -> RegeneratedCurves:
    def build(s: Session, panel: Any, members: Any) -> Any:
        return xsmom_pit_strategy(members)
    return _tr_curves(session, "xsmom-pit-tr", build)


def _pead_curves(session: Session) -> RegeneratedCurves:
    def build(s: Session, panel: Any, members: Any) -> Any:
        earnings, _cov = load_pead_signals(s, sorted(members), panel.dates,
                                           members)
        return pead_pit_strategy(members, earnings)
    return _tr_curves(session, "pead-sue-tr", build)


REGENERATORS: dict[str, Callable[[Session], RegeneratedCurves]] = {
    "xsmom-pit-tr": _xsmom_curves,
    "pead-sue-tr": _pead_curves,
}


def load_strategy_row(session: Session, family: str) -> Mapping[str, Any] | None:
    return session.execute(text(
        "SELECT id, family, state, tolerance_bands FROM quant.strategies "
        "WHERE family = :f ORDER BY created_at DESC LIMIT 1"),
        {"f": family}).mappings().first()


def apply_proposal(session: Session, clock: Clock, *, strategy_id: Any,
                   family: str, proposed: dict[str, Any]) -> str | None:
    """Write `proposed` to quant.strategies.tolerance_bands iff NO band
    loosens against the row AS STORED RIGHT NOW (re-read inside the write
    path — defense in depth on top of band_derivation's tighten-only rule).
    Returns a refusal line (nothing written) or None on success."""
    stored = session.execute(text(
        "SELECT tolerance_bands FROM quant.strategies WHERE id = :i"),
        {"i": strategy_id}).scalar_one()
    if not isinstance(stored, dict):
        return (f"REFUSED: {family} stored tolerance_bands is not an object "
                "— a banded approval without bands is a governance breach; "
                "nothing written")
    for key in (DD_BAND_KEY, EXCESS_BAND_KEY):
        try:
            current = float(stored[key])
            new = float(proposed[key])
        except (KeyError, TypeError, ValueError) as e:
            return (f"REFUSED: band {key} missing or malformed ({e!r}); "
                    "nothing written")
        if new < current:
            return (f"REFUSED: proposed {key}={new} would LOOSEN the stored "
                    f"{current} — loosening any band requires a new signed "
                    "ADR (ADR-0010/0013 tighten-only); nothing written")
    session.execute(text(
        "UPDATE quant.strategies SET tolerance_bands = CAST(:b AS jsonb) "
        "WHERE id = :i"), {"b": json.dumps(proposed), "i": strategy_id})
    PostgresAuditLog(session, clock).append(
        event_type=BANDS_DERIVED_EVENT, entity_type="strategy",
        entity_id=str(strategy_id), actor_type="dcp", actor_id="derive_bands",
        payload={"family": family,
                 "old": stored,                     # verbatim, per the spec
                 "new": proposed,                   # verbatim
                 "tighten_only": "enforced twice: band_derivation._decide + "
                                 "apply_proposal stored-row re-check",
                 "decision_refs": ["board item 7", "ADR-0010 §guardrails",
                                   "ADR-0013 §guardrails"]})
    return None


def run(session: Session, clock: Clock, *, family: str, apply: bool,
        out: Callable[[str], None] = print) -> int:
    regen = REGENERATORS.get(family)
    if regen is None:
        out(f"REFUSED: unknown family {family!r} — derivable families: "
            f"{sorted(REGENERATORS)}")
        return 1
    row = load_strategy_row(session, family)
    if row is None:
        out(f"REFUSED: quant.strategies has no {family} row — bands are "
            "derived for an APPROVED strategy, never invented for one "
            "(run the approval tool first)")
        return 1
    standing = row["tolerance_bands"]
    if not isinstance(standing, dict):
        out(f"REFUSED: {family} tolerance_bands is not an object — fix the "
            "approval row before deriving")
        return 1

    out(f"regenerating {family} backtest curve in-process ...")
    curves = regen(session)
    derived: DerivedBands = derive_proposed_bands(
        dates=curves.dates, strategy_curve=curves.strategy,
        spy_curve=curves.spy, provisional=standing, curve_note=curves.note)

    out(f"{family} ({row['state']}): {len(curves.dates)} sessions "
        f"{curves.dates[0]}..{curves.dates[-1]}")
    out(f"  excess: {derived.excess_windows} overlapping 126s windows, "
        f"1st percentile {derived.derived_excess_floor_pp:+.2f}pp")
    out(f"  drawdown: full-window {derived.full_max_dd:.4f}, rolling-252 p1 "
        f"{derived.rolling_dd_p1:.4f}, margined floor "
        f"{derived.derived_dd_floor:.4f}")
    out(f"  cusum: mean daily excess {derived.mean_daily_excess:+.6f}, "
        f"sigma {derived.sigma_daily_excess:.6f} (k=0.5σ, h=5σ)")
    out("  before/after (tighten-only):")
    for d in derived.decisions:
        verdict = "TIGHTENED" if d.tightened else "KEPT (standing)"
        out(f"    {d.key}: standing {d.provisional:g} | derived "
            f"{d.derived:.6f} -> chosen {d.chosen:.6f}  [{verdict}]")
        out(f"      {d.note}")

    if not apply:
        out("dry run — nothing written (re-run with --apply to update "
            "quant.strategies and audit the change)")
        return 0
    err = apply_proposal(session, clock, strategy_id=row["id"], family=family,
                         proposed=derived.tolerance_bands)
    if err is not None:
        out(err)
        return 1
    out(f"APPLIED: {family} tolerance_bands are now derived "
        f"(provisional=false); audit event {BANDS_DERIVED_EVENT} appended "
        "with old/new verbatim")
    return 0


def main(argv: list[str] | None = None) -> int:
    from atlas.core.db import session_scope

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", required=True,
                    help="strategy family (xsmom-pit-tr | pead-sue-tr)")
    ap.add_argument("--apply", action="store_true",
                    help="write the derived bands + audit event (default: "
                         "dry-run print only)")
    a = ap.parse_args(argv)
    with session_scope() as s:
        return run(s, SystemClock(), family=a.family, apply=a.apply)


if __name__ == "__main__":
    raise SystemExit(main())
