"""Strategy approval gate (Doc 02 §7.4 + ADR-0002): approval is a function of
ARTIFACTS, not arguments. Missing any required artifact -> refusal with reasons.
Writes quant.validation_reports and transitions the strategy row."""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.dcp.backtest.registry import trial_count
from atlas.dcp.backtest.validation import GateReport
from atlas.dcp.backtest.walkforward import WalkForwardResult


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    reasons: list[str]


def evaluate_approval(session: Session, *, family: str,
                      gate: GateReport | None,
                      wf: WalkForwardResult | None,
                      oos_untouched_attested: bool) -> ApprovalDecision:
    reasons: list[str] = []
    n = trial_count(session, family)
    if n == 0:
        reasons.append("no trials registered — every backtest must be in the registry")
    if gate is None:
        reasons.append("missing null-model gate report")
    elif not gate.passed:
        reasons.append(f"gate failed: {'; '.join(gate.reasons)}")
    elif gate.n_trials != n:
        reasons.append(f"gate computed with n_trials={gate.n_trials} but registry has {n} "
                       "— deflated Sharpe must use the true count")
    if wf is None:
        reasons.append("missing purged walk-forward result")
    elif wf.positive_folds < len(wf.fold_results) // 2 + 1:
        reasons.append(f"walk-forward: only {wf.positive_folds}/{len(wf.fold_results)} "
                       "folds positive")
    if not oos_untouched_attested:
        reasons.append("OOS holdout not attested as untouched during development")
    return ApprovalDecision(approved=not reasons, reasons=reasons)


def record_and_transition(session: Session, *, strategy_id: str, backtest_id: str | None,
                          decision: ApprovalDecision, checklist: dict[str, object]) -> None:
    verdict = "approve" if decision.approved else "reject"
    session.execute(text(
        "INSERT INTO quant.validation_reports "
        "(strategy_id, backtest_id, checklist, verdict, reasons) "
        "VALUES (:sid, :bid, CAST(:c AS jsonb), :v, :r)"),
        {"sid": strategy_id, "bid": backtest_id, "c": json.dumps(checklist),
         "v": verdict, "r": "; ".join(decision.reasons)})
    if decision.approved:
        session.execute(text(
            "UPDATE quant.strategies SET state='validated' "
            "WHERE id=:sid AND state='backtested'"), {"sid": strategy_id})
