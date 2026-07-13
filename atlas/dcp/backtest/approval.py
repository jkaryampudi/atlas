"""Strategy approval gate (Doc 02 §7.4 + ADR-0002): approval is a function of
ARTIFACTS, not arguments. Missing any required artifact -> refusal with reasons.
Writes quant.validation_reports and transitions the strategy row.

State machine: backtested -> validated (record_and_transition, artifact-gated)
-> paper (transition_to_paper, Principal-signed). Nothing here reaches 'live';
live is Phase 7 and needs its own signed machinery.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.backtest.registry import trial_count


class GateArtifact(Protocol):
    """Any null-model gate report (single-symbol GateReport or the portfolio
    PortfolioGateReport) — approval only reads the verdict fields."""
    @property
    def passed(self) -> bool: ...
    @property
    def reasons(self) -> list[str]: ...
    @property
    def n_trials(self) -> int: ...


class WalkForwardArtifact(Protocol):
    """Any purged walk-forward result — approval only reads fold counts."""
    @property
    def fold_results(self) -> Sequence[object]: ...
    @property
    def positive_folds(self) -> int: ...


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    reasons: list[str]


def evaluate_approval(session: Session, *, family: str,
                      gate: GateArtifact | None,
                      wf: WalkForwardArtifact | None,
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


def transition_to_paper(session: Session, audit: PostgresAuditLog, *,
                        strategy_id: str, approved_by: str,
                        decision_ref: str, clock: Clock) -> None:
    """'validated' -> 'paper': the Principal's signature, never automatic.

    Guards fail closed: the row must be exactly 'validated' and its LATEST
    validation report must be an 'approve' — a rejected or missing report
    cannot be ridden over by this call. approved_by names the human;
    decision_ref points at the durable record of the decision (ADR). The
    transition is a material action and lands on the audit chain.
    """
    state = session.execute(text(
        "SELECT state FROM quant.strategies WHERE id = :sid"),
        {"sid": strategy_id}).scalar()
    if state is None:
        raise ValueError(f"strategy {strategy_id} does not exist")
    if state != "validated":
        raise ValueError(f"strategy {strategy_id} is '{state}', not 'validated' "
                         "— paper approval requires the artifact-gated "
                         "validation transition first")
    verdict = session.execute(text(
        "SELECT verdict FROM quant.validation_reports "
        "WHERE strategy_id = :sid ORDER BY created_at DESC LIMIT 1"),
        {"sid": strategy_id}).scalar()
    if verdict != "approve":
        raise ValueError(f"latest validation report verdict is {verdict!r}, "
                         "not 'approve' — refusing paper transition")
    session.execute(text(
        "UPDATE quant.strategies SET state='paper', approved_by=:by, "
        "approved_at=:ts WHERE id=:sid AND state='validated'"),
        {"sid": strategy_id, "by": approved_by, "ts": clock.now()})
    row = session.execute(text(
        "SELECT family, name, version FROM quant.strategies WHERE id=:sid"),
        {"sid": strategy_id}).mappings().one()
    audit.append(
        event_type="quant.strategy.approved_paper", entity_type="strategy",
        entity_id=strategy_id, actor_type="human", actor_id=approved_by,
        payload={"family": row["family"], "name": row["name"],
                 "version": row["version"], "new_state": "paper",
                 "decision_ref": decision_ref})
