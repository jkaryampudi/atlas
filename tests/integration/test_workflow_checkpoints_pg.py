"""Workflow checkpoints (ADR-0005 pattern 3): kill mid-run, resume the same
run_id — no completed node executes twice, and the audit chain carries exactly
one completion event per node."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from atlas.core.workflow import Node, WorkflowRunner
from tests.conftest import requires_pg

pytestmark = requires_pg
CLOCK = FrozenClock(datetime(2026, 7, 11, 22, 0, tzinfo=UTC))


def _audit(s):
    return PostgresAuditLog(s, CLOCK)


def _clean(s, run_id: str) -> None:
    s.execute(text("DELETE FROM workflow.workflow_node_results WHERE run_id = :r"),
              {"r": run_id})
    s.execute(text("DELETE FROM workflow.workflow_runs WHERE run_id = :r"),
              {"r": run_id})


def test_kill_mid_run_then_resume_executes_no_node_twice(clean_audit):
    s = clean_audit
    _clean(s, "wf-kill-resume")
    calls = {"a": 0, "b": 0, "c": 0}
    boom = {"on": True}

    def node_a() -> str:
        calls["a"] += 1
        return "ref-a"

    def node_b() -> str:
        calls["b"] += 1
        if boom["on"]:
            raise RuntimeError("simulated kill mid-run")
        return "ref-b"

    def node_c() -> str:
        calls["c"] += 1
        return "ref-c"

    nodes = [Node("a", node_a), Node("b", node_b), Node("c", node_c)]
    runner = WorkflowRunner(s, _audit(s), CLOCK)

    with pytest.raises(RuntimeError, match="simulated kill"):
        runner.run("wf-kill-resume", nodes)
    assert s.execute(text("SELECT status FROM workflow.workflow_runs "
                          "WHERE run_id='wf-kill-resume'")).scalar() == "failed"
    assert calls == {"a": 1, "b": 1, "c": 0}  # c never started

    boom["on"] = False
    results = runner.run("wf-kill-resume", nodes)  # resume same run_id
    assert results == {"a": "ref-a", "b": "ref-b", "c": "ref-c"}
    # the completed node 'a' was NOT re-executed; only b (retry) and c ran
    assert calls == {"a": 1, "b": 2, "c": 1}
    assert s.execute(text("SELECT status FROM workflow.workflow_runs "
                          "WHERE run_id='wf-kill-resume'")).scalar() == "completed"
    # audit chain: exactly one completion event per node
    counts = s.execute(text(
        "SELECT payload->>'node', count(*) FROM audit.decision_events "
        "WHERE event_type='workflow.node.completed' "
        "GROUP BY 1 ORDER BY 1")).all()
    assert [(r[0], r[1]) for r in counts] == [("a", 1), ("b", 1), ("c", 1)]


def test_completed_run_is_fully_skipped(clean_audit):
    s = clean_audit
    _clean(s, "wf-idempotent")
    calls = {"n": 0}

    def node() -> str:
        calls["n"] += 1
        return "ref"

    runner = WorkflowRunner(s, _audit(s), CLOCK)
    runner.run("wf-idempotent", [Node("only", node)])
    before = s.execute(text("SELECT count(*) FROM audit.decision_events "
                            "WHERE event_type='workflow.node.completed'")).scalar()
    results = runner.run("wf-idempotent", [Node("only", node)])
    after = s.execute(text("SELECT count(*) FROM audit.decision_events "
                           "WHERE event_type='workflow.node.completed'")).scalar()
    assert calls["n"] == 1           # completed node never re-executed
    assert results == {"only": "ref"}
    assert before == after           # skipped nodes emit no new events
