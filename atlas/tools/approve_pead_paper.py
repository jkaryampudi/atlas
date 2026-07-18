"""Execute the Principal's PAPER approval of PEAD/SUE (ADR-0013) — one-shot tool.

Mirrors atlas/tools/approve_xsmom_paper.py exactly (approval is a function of
ARTIFACTS regenerated live, per Doc 02 §7.4): re-runs the deterministic
total-return PIT backtest, feeds the gate + purged-walk-forward artifacts
through evaluate_approval, and on approval writes the quant.strategies row and
transitions backtested -> validated -> paper with the Principal's name and the
ADR reference on the audit chain.

PEAD is approved DESPITE recorded robustness concerns (ADR-0013): the
pre-committed 2016 kill trial FAILED, the edge beats SPY at only 4/25 endpoints
(all terminal), and it overlaps momentum (not orthogonal). The full-window gate
PASSES; the artifact-level evaluate_approval does not read the kill trial or the
endpoint exhibit, so the caveats live in ADR-0013 and in the strategy spec's
caveats_ref — they are recorded, not enforced away.

Bands are TIGHTER than momentum's (ADR-0013): DD -45% (backtest -41.17%),
trailing-126-session excess vs SPY TR -20pp (shorter leash — the edge is
unproven out of its recent window).

Usage:
    python -m atlas.tools.approve_pead_paper \
        --approved-by "Jay Karyampudi (Principal)" \
        --decision-ref ADR-0013 --attest-oos-untouched
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
from atlas.dcp.backtest.pead_pit_run import run_pead_pit
from atlas.dcp.signals.pead import v1 as pead_v1

# Tighter-than-momentum demotion bands (ADR-0013). The edge is recent and
# concentrated, so the excess band is a shorter leash than momentum's -25pp.
# Tighten-only: loosening requires a new signed ADR.
TOLERANCE_BANDS: dict[str, object] = {
    "provisional": True,
    "demote_to": "suspended",
    "max_drawdown_from_sleeve_peak": -0.45,
    "trailing_126_session_excess_vs_spy_tr_pp": -20.0,
    "derivation": "backtest maxDD -41.17% + margin; excess floor TIGHTER than "
                  "momentum (edge is recent/concentrated); tighten-only (ADR-0013)",
}

FAMILY = "pead-sue-tr"
NAME = "pead_sue"
VERSION = "1.0.0"


def _code_sha() -> str:
    """Pin the exact signal recipe the approval covers."""
    return hashlib.sha256(Path(pead_v1.__file__).read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--approved-by", required=True)
    ap.add_argument("--decision-ref", required=True)
    ap.add_argument("--attest-oos-untouched", action="store_true",
                    help="Principal attests the evaluation window was not used "
                         "for recipe development (textbook Foster-Olsen-Shevlin "
                         "SUE; the 2016 kill window was pre-committed).")
    ap.add_argument("--paths", type=int, default=1000)
    a = ap.parse_args(argv)

    clock = SystemClock()
    with session_scope() as s:
        audit = PostgresAuditLog(s, clock)
        already = s.execute(text(
            "SELECT id, state FROM quant.strategies WHERE family=:f"),
            {"f": FAMILY}).mappings().first()
        if already is not None:
            print(f"REFUSED: quant.strategies already has {FAMILY} in state "
                  f"'{already['state']}' — approvals are not re-runnable")
            return 1

        print("regenerating artifacts: PEAD total-return PIT backtest "
              f"({a.paths} null paths) ...", flush=True)
        run = run_pead_pit(s, audit, paths=a.paths, total_return=True)
        g = run.gate
        print(f"gate: passed={g.passed} strategy={g.strategy_return:+.2%} "
              f"SPY(TR)={g.spy_bh_return:+.2%} p={g.null_p_value:.3f} "
              f"DSR={g.dsr:.3f} n_trials={g.n_trials}")
        print(f"walk-forward: {run.wf.positive_folds}/"
              f"{len(run.wf.fold_results)} folds positive")

        decision = evaluate_approval(
            s, family=run.family, lineage=run.lineage, gate=g, wf=run.wf,
            oos_untouched_attested=a.attest_oos_untouched)
        if not decision.approved:
            print("REFUSED — approval gate reasons:")
            for r in decision.reasons:
                print(f"  - {r}")
            return 1

        spec = {**pead_v1.SPEC,
                "validated_on": "point-in-time S&P 500 (pead-sue-tr, "
                                "total-return vs SPY TR)",
                "trading_universe": "ADR-0007 (S&P 100 + India sleeve) — "
                                    "implementable-variant backtest OPEN",
                "trial_id": run.trial_id,
                "caveats_ref": a.decision_ref,
                "caveats": "2016 kill trial FAILED; 4/25 endpoints beat SPY "
                           "(all terminal); overlaps momentum (not orthogonal) "
                           "— see ADR-0013"}
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
                       "gate_passed": g.passed, "null_p": g.null_p_value,
                       "dsr": g.dsr, "wf_positive_folds": run.wf.positive_folds,
                       "kill_trial": "FAILED (see ADR-0013)",
                       "endpoints_beating_spy": "4/25 (terminal)",
                       "report": "docs/reports/pead-sue-total-return-2026-07.md",
                       "decision_ref": a.decision_ref})
        transition_to_paper(s, audit, strategy_id=str(sid),
                            approved_by=a.approved_by,
                            decision_ref=a.decision_ref, clock=clock)
        print(f"APPROVED FOR PAPER: {run.family}/{NAME} v{VERSION} "
              f"strategy_id={sid}")
        print(f"  approved_by: {a.approved_by}  ref: {a.decision_ref}")
        print("  bands: DD -45% / excess -20pp (tighter than momentum); "
              "caveats on ADR-0013")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
