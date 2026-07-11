"""Resumable workflow checkpoints (ADR-0005 pattern 3).

Each node persists its result BEFORE the next node runs; re-running the same
run_id skips nodes already 'done', so a completed node never executes twice.
Every executed node emits exactly one workflow.node.completed audit event —
skipped nodes emit nothing, so the chain shows one event per node per run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock


@dataclass(frozen=True)
class Node:
    name: str
    fn: Callable[[], str]  # executes the step; returns an output reference


class WorkflowRunner:
    def __init__(self, session: Session, audit: PostgresAuditLog, clock: Clock) -> None:
        self._s = session
        self._audit = audit
        self._clock = clock

    def _node_state(self, run_id: str, name: str) -> tuple[str, str | None] | None:
        row = self._s.execute(text(
            "SELECT status, output_ref FROM workflow.workflow_node_results "
            "WHERE run_id = :r AND node_name = :n"), {"r": run_id, "n": name}).first()
        return (row.status, row.output_ref) if row else None

    def _record_node(self, run_id: str, name: str, status: str, ref: str | None) -> None:
        self._s.execute(text(
            "INSERT INTO workflow.workflow_node_results "
            "(run_id, node_name, status, output_ref, completed_at) "
            "VALUES (:r, :n, :st, :ref, :at) "
            "ON CONFLICT (run_id, node_name) DO UPDATE SET "
            "  status = :st, output_ref = :ref, completed_at = :at"),
            {"r": run_id, "n": name, "st": status, "ref": ref,
             "at": self._clock.now()})

    def _set_run(self, run_id: str, status: str) -> None:
        self._s.execute(text(
            "INSERT INTO workflow.workflow_runs (run_id, started_at, status) "
            "VALUES (:r, :at, :st) "
            "ON CONFLICT (run_id) DO UPDATE SET status = :st, "
            "  completed_at = CASE WHEN :st IN ('completed','failed') "
            "                      THEN CAST(:at AS timestamptz) ELSE NULL END"),
            {"r": run_id, "at": self._clock.now(), "st": status})

    def run(self, run_id: str, nodes: list[Node]) -> dict[str, str | None]:
        """Execute nodes in order, checkpointing each. Resume-safe: 'done' nodes
        are skipped and their persisted output_ref returned."""
        self._set_run(run_id, "running")
        results: dict[str, str | None] = {}
        for node in nodes:
            state = self._node_state(run_id, node.name)
            if state is not None and state[0] == "done":
                results[node.name] = state[1]
                continue
            try:
                ref = node.fn()
            except Exception as e:
                self._record_node(run_id, node.name, "failed", str(e)[:200])
                self._set_run(run_id, "failed")
                self._audit.append(event_type="workflow.node.failed",
                                   entity_type="workflow", entity_id=run_id,
                                   actor_type="scheduler", actor_id="workflow_runner",
                                   payload={"node": node.name, "error": str(e)[:200]})
                raise
            self._record_node(run_id, node.name, "done", ref)
            self._audit.append(event_type="workflow.node.completed",
                               entity_type="workflow", entity_id=run_id,
                               actor_type="scheduler", actor_id="workflow_runner",
                               payload={"node": node.name, "output_ref": ref})
            results[node.name] = ref
        self._set_run(run_id, "completed")
        return results
