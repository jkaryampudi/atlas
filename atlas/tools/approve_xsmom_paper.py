"""Execute the Principal's PAPER approval of xsmom (ADR-0010) — one-shot tool.

This is the fund's first strategy approval. It follows Doc 02 §7.4 to the
letter: the approval is a function of ARTIFACTS regenerated live, never of
arguments —

  1. re-runs the deterministic total-return PIT backtest in-process
     (`run_xsmom_pit(total_return=True)`, seed pinned), which registers a
     fresh trial (the registry only ever grows) and yields the typed gate +
     purged-walk-forward artifacts;
  2. feeds them through `evaluate_approval` (thresholds live in validation,
     imported, never restated). ANY refusal reason aborts with exit 1 and no
     row is written;
  3. inserts the quant.strategies row (live recipe SPEC, code sha of the
     signal module, PROVISIONAL tolerance bands per ADR-0010 — bands may only
     ever tighten), records the validation report, and transitions
     backtested -> validated -> paper with the Principal's name and the ADR
     reference on the audit chain.

Known, recorded caveats (ADR-0010): endpoint concentration (8/25 and 3/25
month-end wins vs SPY TR), early-window membership undercount, and the
validated-universe (PIT S&P 500) vs trading-universe (ADR-0007 S&P 100 +
India sleeve) gap — board item 5 (implementable-variant backtest) stays open.

Usage (deliberate, spelled-out flags — no defaults that could sign silently):

    python -m atlas.tools.approve_xsmom_paper \
        --approved-by "Jay Karyampudi (Principal)" \
        --decision-ref ADR-0010 --attest-oos-untouched
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import SystemClock
from atlas.core.db import session_scope
from atlas.dcp.backtest.approval import (
    evaluate_approval,
    record_and_transition,
    transition_to_paper,
)
from atlas.dcp.backtest.xsmom_pit_run import run_xsmom_pit
from atlas.dcp.signals.xsmom import v1 as xsmom_v1

# Provisional demotion bands (ADR-0010 §bands). Derivation: the validated
# backtest's max drawdown was -36.9%; a paper sleeve that exceeds -40% from
# its own peak is outside the record. -25pp trailing-126-session excess vs
# SPY total return is likewise beyond any 6-month stretch in 2012-2026.
# "Provisional" = the approval-contract build (board item 7) must derive
# exact percentile bands from the stored equity curve and may only TIGHTEN
# these; loosening requires a new signed ADR.
TOLERANCE_BANDS: dict[str, object] = {
    "provisional": True,
    "demote_to": "suspended",
    "max_drawdown_from_sleeve_peak": -0.40,
    "trailing_126_session_excess_vs_spy_tr_pp": -25.0,
    "derivation": "backtest maxDD -36.9% + margin; six-month excess floor "
                  "outside the 2012-2026 record; tighten-only (ADR-0010)",
}

FAMILY = "xsmom-pit-tr"
NAME = "xsmom_pit"
VERSION = "1.0.0"


def _code_sha() -> str:
    """Pin the exact signal recipe the approval covers (prompts-are-code
    discipline applied to strategy code)."""
    src = Path(xsmom_v1.__file__).read_bytes()
    return hashlib.sha256(src).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--approved-by", required=True)
    ap.add_argument("--decision-ref", required=True)
    ap.add_argument("--attest-oos-untouched", action="store_true",
                    help="Principal attests the evaluation window was not "
                         "used for recipe development (the recipe is J&T "
                         "12-1 verbatim; the 2016 kill window was "
                         "pre-committed before it ran)")
    ap.add_argument("--paths", type=int, default=1000)
    a = ap.parse_args(argv)

    clock = SystemClock()
    with session_scope() as s:
        audit = PostgresAuditLog(s, clock)
        already = s.execute(text(
            "SELECT id, state FROM quant.strategies WHERE family=:f"),
            {"f": FAMILY}).mappings().first()
        if already is not None:
            print(f"REFUSED: quant.strategies already has {FAMILY} "
                  f"in state '{already['state']}' — approvals are not "
                  "re-runnable; demote/promote explicitly instead")
            return 1

        print("regenerating artifacts: total-return PIT backtest "
              f"({a.paths} null paths, pinned seed) ...", flush=True)
        run = run_xsmom_pit(s, audit, paths=a.paths, total_return=True)
        g = run.gate
        print(f"gate: passed={g.passed} strategy={g.strategy_return:+.2%} "
              f"SPY(TR)={g.spy_bh_return:+.2%} p={g.null_p_value:.3f} "
              f"DSR={g.dsr:.3f} n_trials={g.n_trials}")
        print(f"walk-forward: {run.wf.positive_folds}/"
              f"{len(run.wf.fold_results)} folds positive")

        decision = evaluate_approval(
            s, family=run.family, gate=g, wf=run.wf,
            oos_untouched_attested=a.attest_oos_untouched)
        if not decision.approved:
            print("REFUSED — approval gate reasons:")
            for r in decision.reasons:
                print(f"  - {r}")
            return 1

        spec = {**xsmom_v1.SPEC,
                "validated_on": "point-in-time S&P 500 (xsmom-pit-tr, "
                                "total-return vs SPY TR)",
                "trading_universe": "ADR-0007 (S&P 100 + India sleeve) — "
                                    "implementable-variant backtest is board "
                                    "item 5, OPEN",
                "trial_id": run.trial_id,
                "caveats_ref": a.decision_ref}
        sid = s.execute(text(
            "INSERT INTO quant.strategies "
            "(family, name, version, spec, code_sha, tolerance_bands, state) "
            "VALUES (:f, :n, :v, CAST(:s AS jsonb), :c, CAST(:b AS jsonb), "
            "'backtested') RETURNING id"),
            {"f": run.family, "n": NAME, "v": VERSION,
             "s": json.dumps(spec), "c": _code_sha(),
             "b": json.dumps(TOLERANCE_BANDS)}).scalar_one()
        record_and_transition(
            s, strategy_id=str(sid), backtest_id=None, decision=decision,
            checklist={"artifact": "regenerated in-process this run",
                       "trial_id": run.trial_id,
                       "n_trials_family": run.n_trials,
                       "registry_total_after": run.trials_after_total,
                       "gate_passed": g.passed, "null_p": g.null_p_value,
                       "dsr": g.dsr,
                       "wf_positive_folds": run.wf.positive_folds,
                       "report": "docs/reports/xsmom-pit-total-return-2026-07.md",
                       "decision_ref": a.decision_ref})
        transition_to_paper(s, audit, strategy_id=str(sid),
                            approved_by=a.approved_by,
                            decision_ref=a.decision_ref, clock=clock)
        print(f"APPROVED FOR PAPER: {run.family}/{NAME} v{VERSION} "
              f"strategy_id={sid}")
        print(f"  approved_by: {a.approved_by}  ref: {a.decision_ref}")
        print(f"  code_sha: {_code_sha()[:16]}…  bands: provisional "
              "(tighten-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
