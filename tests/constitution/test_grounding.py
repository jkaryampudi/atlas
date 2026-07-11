"""Grounding verifier red-team (ADR-0005 pattern 2): a number an agent did not
read in its cited evidence is a fabrication and must fail closed."""
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from atlas.agents.roles.cio import committee_memo  # noqa: F401  (suite convention)
from atlas.agents.runtime.grounding import grounding_violations, numeric_tokens
from atlas.agents.runtime.llm import StubClient
from atlas.agents.runtime.runner import AgentRunFailed, run_agent
from atlas.agents.schemas.memo import CommitteeMemo
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg


def _audit(s):
    return PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 11, 6, 0, tzinfo=UTC)))


def _memo(thesis: str) -> str:
    return json.dumps({
        "recommendation": "WATCHLIST", "conviction": "LOW", "thesis": thesis,
        "kill_criteria": ["Capex guidance turns negative", "Customer concentration worsens"],
        "evidence_refs": ["ev-1"], "dissent": "The growth may already be priced in."})


def _run(s, response: str, evidence_body: str):
    return run_agent(
        session=s, audit=_audit(s), client=StubClient([response, response]),
        agent_role="cio", template_rel_path="cio/committee_memo.md",
        context=f"DCP evidence [ev-1]: {evidence_body}",
        output_model=CommitteeMemo,
        input_refs=[{"type": "evidence", "id": "ev-1"}],
        extra_fields={"evidence_available": True},
        evidence_bodies={"ev-1": evidence_body})


def test_fabricated_number_fails_closed_with_audit_event(clean_audit):
    s = clean_audit
    with pytest.raises(AgentRunFailed):
        _run(s, _memo("Revenue grew 47 percent per the cited memo."),
             evidence_body="Revenue growth was robust year over year.")
    statuses = s.execute(text(
        "SELECT DISTINCT status FROM research.agent_runs")).scalars().all()
    assert statuses == ["schema_fail"]  # grounding takes the schema_fail path
    n = s.execute(text("SELECT count(*) FROM audit.decision_events "
                       "WHERE event_type='agent.grounding.failed'")).scalar()
    assert n == 2  # one per attempt, then fail closed


def test_same_number_present_in_cited_evidence_passes(clean_audit):
    s = clean_audit
    memo, _ = _run(s, _memo("Revenue grew 47 percent per the cited memo."),
                   evidence_body="Q3 filing: revenue grew 47 percent year over year.")
    assert memo.recommendation == "WATCHLIST"
    status = s.execute(text("SELECT status FROM research.agent_runs")).scalar()
    assert status == "ok"


def test_number_in_uncited_evidence_is_still_ungrounded(clean_audit):
    """Grounding is per-citation: the number exists in the corpus but the memo
    does not cite the body containing it."""
    s = clean_audit
    response = json.dumps({
        "recommendation": "WATCHLIST", "conviction": "LOW",
        "thesis": "Revenue grew 47 percent per the cited memo.",
        "kill_criteria": ["Capex guidance turns negative", "Customer concentration worsens"],
        "evidence_refs": ["ev-1"], "dissent": "Growth may be priced in."})
    with pytest.raises(AgentRunFailed):
        run_agent(session=s, audit=_audit(s),
                  client=StubClient([response, response]),
                  agent_role="cio", template_rel_path="cio/committee_memo.md",
                  context="DCP evidence [ev-1]: growth was robust.\n"
                          "DCP evidence [ev-2]: revenue grew 47 percent.",
                  output_model=CommitteeMemo,
                  input_refs=[{"type": "evidence", "id": "ev-1"}],
                  extra_fields={"evidence_available": True},
                  evidence_bodies={"ev-1": "growth was robust.",
                                   "ev-2": "revenue grew 47 percent."})


def test_whitelist_rule_ids_and_years_need_no_evidence(clean_audit):
    s = clean_audit
    memo, _ = _run(s, _memo("A breach of L8 or DD2 in 2026 would invalidate the thesis."),
                   evidence_body="No numbers here at all.")
    assert memo.recommendation == "WATCHLIST"


def test_numeric_token_extraction_unit():
    assert numeric_tokens("grew 47 percent in 2026 vs L8 cap") == ["47"]
    assert numeric_tokens("L11 and DD3 are rule ids") == []
    assert numeric_tokens("a 3.5 sigma move") == ["3.5"]


def test_grounding_violations_unit():
    memo = CommitteeMemo(recommendation="WATCHLIST", conviction="LOW",
                         thesis="Margin reached 41 percent.",
                         kill_criteria=["a", "b"], evidence_refs=["e1"],
                         dissent="none of note", evidence_available=True)
    assert grounding_violations(memo, {"e1": "margin reached 41 percent"}) == []
    assert grounding_violations(memo, {"e1": "margin expanded"}) != []
