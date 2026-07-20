"""Strategy approval gate (Doc 02 §7.4 + ADR-0002): approval is a function of
ARTIFACTS, not arguments. Missing any required artifact -> refusal with reasons.
Writes quant.validation_reports and transitions the strategy row.

State machine: backtested -> validated (record_and_transition, artifact-gated)
-> paper (transition_to_paper, Principal-signed). Nothing here reaches 'live';
live is Phase 7 and needs its own signed machinery.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.backtest.registry import lineage_count, trial_count


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


def evaluate_approval(session: Session, *, family: str, lineage: str,
                      gate: GateArtifact | None,
                      wf: WalkForwardArtifact | None,
                      oos_untouched_attested: bool) -> ApprovalDecision:
    """The n-consistency check compares the gate's n_trials against the same
    LINEAGE count the gate must have used (ADR-0016): a gate deflated at the
    old per-family count no longer clears this check once the lineage holds
    more trials — stricter going forward, never retroactive."""
    reasons: list[str] = []
    n = lineage_count(session, lineage)
    if trial_count(session, family) == 0:
        reasons.append("no trials registered — every backtest must be in the registry")
    if gate is None:
        reasons.append("missing null-model gate report")
    elif not gate.passed:
        reasons.append(f"gate failed: {'; '.join(gate.reasons)}")
    elif gate.n_trials != n:
        reasons.append(f"gate computed with n_trials={gate.n_trials} but lineage "
                       f"'{lineage}' has {n} trials "
                       "— deflated Sharpe must use the true lineage count (ADR-0016)")
    if wf is None:
        reasons.append("missing purged walk-forward result")
    elif wf.positive_folds < len(wf.fold_results) // 2 + 1:
        reasons.append(f"walk-forward: only {wf.positive_folds}/{len(wf.fold_results)} "
                       "folds positive")
    if not oos_untouched_attested:
        reasons.append("OOS holdout not attested as untouched during development")
    return ApprovalDecision(approved=not reasons, reasons=reasons)


def strategy_identity(session: Session, strategy_id: str) -> dict[str, str] | None:
    """The strategy's executable identity used to bind a validation artifact to
    the exact code+config it validated (ADR-0018): code_sha (the hashed signal
    recipe), version, and spec_hash (a canonical sha256 of the spec jsonb). Both
    the write-time stamp and the read-time compare hash the SAME jsonb column via
    the same canonical form, so there is no round-trip false mismatch — only a
    genuine change to the strategy's code/version/config produces a mismatch.
    Returns None if the strategy row is absent."""
    row = session.execute(text(
        "SELECT code_sha, version, spec FROM quant.strategies WHERE id = :sid"),
        {"sid": strategy_id}).mappings().first()
    if row is None:
        return None
    spec = row["spec"] if isinstance(row["spec"], dict) else {}
    spec_hash = hashlib.sha256(
        json.dumps(spec, sort_keys=True).encode()).hexdigest()
    return {"code_sha": row["code_sha"] or "", "version": row["version"] or "",
            "spec_hash": spec_hash}


def record_and_transition(session: Session, *, strategy_id: str, backtest_id: str | None,
                          decision: ApprovalDecision, checklist: dict[str, object]) -> None:
    verdict = "approve" if decision.approved else "reject"
    # Stamp the strategy's executable identity onto the report so the fail-closed
    # promotion gate can later reject an artifact that validated a different
    # code/version/config (ADR-0018). Additive metadata — no gate calc changes.
    stamped = {**checklist, "_identity": strategy_identity(session, strategy_id)}
    session.execute(text(
        "INSERT INTO quant.validation_reports "
        "(strategy_id, backtest_id, checklist, verdict, reasons) "
        "VALUES (:sid, :bid, CAST(:c AS jsonb), :v, :r)"),
        {"sid": strategy_id, "bid": backtest_id, "c": json.dumps(stamped),
         "v": verdict, "r": "; ".join(decision.reasons)})
    if decision.approved:
        session.execute(text(
            "UPDATE quant.strategies SET state='validated' "
            "WHERE id=:sid AND state='backtested'"), {"sid": strategy_id})


def require_signed_validation_artifact(session: Session, strategy_id: str) -> None:
    """Fail-closed promotion gate (ADR-0018). Promotion to 'paper' requires a
    SIGNED validation artifact: the strategy's LATEST quant.validation_reports
    row must have verdict='approve'. A missing report or a 'reject' refuses —
    there is deliberately NO override/convenience parameter on this call path.

    Freshness: if the strategy was ever downgraded to research_shadow
    (quant.strategies.shadowed_at is set), the 'approve' report must have been
    created STRICTLY AFTER the downgrade. The stale pre-downgrade approval that
    the independent review rejected (deployed price-return signal != validated
    total-return signal; DSR ~0.85 at lineage count) can never re-promote the
    executable — a NEW signed validation artifact is mandatory.
    """
    row = session.execute(text(
        "SELECT shadowed_at FROM quant.strategies WHERE id = :sid"),
        {"sid": strategy_id}).mappings().first()
    if row is None:
        raise ValueError(f"strategy {strategy_id} does not exist")
    report = session.execute(text(
        "SELECT verdict, created_at, checklist FROM quant.validation_reports "
        "WHERE strategy_id = :sid ORDER BY created_at DESC LIMIT 1"),
        {"sid": strategy_id}).mappings().first()
    if report is None:
        raise ValueError(
            "no validation report on record — promotion requires a signed "
            "validation artifact (ADR-0018 fail-closed)")
    if report["verdict"] != "approve":
        raise ValueError(
            f"latest validation report verdict is {report['verdict']!r}, not "
            "'approve' — refusing paper transition")
    if row["shadowed_at"] is not None and report["created_at"] <= row["shadowed_at"]:
        raise ValueError(
            "the latest 'approve' validation report predates the research_shadow "
            "downgrade — a NEW signed validation artifact is required after the "
            "ADR-0018 downgrade; the stale approval cannot be reused")
    # Identity compatibility (ADR-0018): the artifact must have validated the
    # strategy's CURRENT executable identity. A report joined by strategy_id but
    # stamped with a different code_sha / version / spec_hash validated a
    # different executable and cannot promote this one — fail closed. A
    # downgraded strategy's re-promotion artifact MUST carry a stamped identity
    # (a legacy unstamped report cannot lift a research_shadow row).
    checklist = report["checklist"] if isinstance(report["checklist"], dict) else {}
    stamped = checklist.get("_identity")
    if row["shadowed_at"] is not None and not stamped:
        raise ValueError(
            "re-promotion of a research_shadow strategy requires an "
            "identity-stamped validation artifact (ADR-0018) — the report does "
            "not pin code_sha/version/spec")
    if stamped is not None:
        current = strategy_identity(session, strategy_id)
        if stamped != current:
            raise ValueError(
                "validation artifact identity does not match the strategy's "
                f"current identity (artifact={stamped}, strategy={current}) — the "
                "artifact validated a different code/version/config; refusing "
                "promotion")


def transition_to_paper(session: Session, audit: PostgresAuditLog, *,
                        strategy_id: str, approved_by: str,
                        decision_ref: str, clock: Clock) -> None:
    """'validated' -> 'paper': the Principal's signature, never automatic.

    Guards fail closed: the row must be exactly 'validated' (a research_shadow /
    suspended / backtested strategy cannot jump the queue) and it must carry a
    fresh signed validation artifact (require_signed_validation_artifact) — a
    rejected, missing, or stale-pre-downgrade report cannot be ridden over by
    this call, and there is no override flag. approved_by names the human;
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
    require_signed_validation_artifact(session, strategy_id)
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
