"""Shadow runs (Constitution 7.2, ADR-0005 pattern 4): logged, non-actionable."""
import json
from datetime import UTC, datetime

from sqlalchemy import text

from atlas.agents.runtime.llm import StubClient
from atlas.agents.runtime.runner import run_agent
from atlas.agents.schemas.memo import CommitteeMemo
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg

GOOD = json.dumps({
    "recommendation": "WATCHLIST", "conviction": "LOW",
    "thesis": "Plausible setup pending quant confirmation.",
    "kill_criteria": ["Capex guidance turns negative", "Customer concentration worsens"],
    "evidence_refs": [], "dissent": "May already be priced in."})


def _audit(s):
    return PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 11, 6, 0, tzinfo=UTC)))


def _run(s, shadow: bool):
    return run_agent(session=s, audit=_audit(s), client=StubClient([GOOD]),
                     agent_role="cio", template_rel_path="cio/committee_memo.md",
                     context="Candidate: AVGO", output_model=CommitteeMemo,
                     input_refs=[], shadow_mode=shadow)


def test_shadow_run_is_logged_and_marked_non_actionable(clean_audit):
    s = clean_audit
    memo, run_id = _run(s, shadow=True)
    assert memo.recommendation == "WATCHLIST"  # output exists for comparison
    row = s.execute(text("SELECT status, shadow, model FROM research.agent_runs "
                         "WHERE id = :i"), {"i": run_id}).one()
    assert row.status == "ok" and row.shadow is True
    assert row.model  # Constitution 7.2: the model string is recorded per run
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type='agent.run.completed'")).scalar()
    assert payload["shadow"] is True


def test_normal_run_is_not_shadow(clean_audit):
    s = clean_audit
    _, run_id = _run(s, shadow=False)
    shadow = s.execute(text("SELECT shadow FROM research.agent_runs WHERE id = :i"),
                       {"i": run_id}).scalar()
    assert shadow is False
