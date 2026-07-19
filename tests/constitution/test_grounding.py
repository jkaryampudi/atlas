"""Grounding verifier red-team (ADR-0005 pattern 2): a number an agent did not
read in its cited evidence is a fabrication and must fail closed."""
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from atlas.agents.roles.cio import committee_memo  # noqa: F401  (suite convention)
from atlas.agents.runtime.grounding import (
    corpus_numeric_tokens,
    grounding_violations,
    numeric_tokens,
)
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
        session=s, audit=_audit(s), client=StubClient([response, response, response]),
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
    assert n == 3  # one grounding failure per attempt (3), then fail closed


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
                  client=StubClient([response, response, response]),
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


# ---- token-boundary red team (desk-review 2026-07 item 4) ------------------
# The exact bypass: substring matching let a narrative '20' ground against the
# '20' inside 'SMA20', and small integers leak out of ISO dates. The verifier
# now compares SET MEMBERSHIP over tokens from one shared boundary-aware
# tokenizer. These tests pin the bypass DEAD and pin that legitimate
# standalone numbers still ground.

def test_redteam_bare_20_grounded_only_by_sma20_now_kills(clean_audit):
    s = clean_audit
    with pytest.raises(AgentRunFailed):
        _run(s, _memo("Price sits above the 20 session average."),
             evidence_body="SMA20 543.21 and rising.")
    n = s.execute(text("SELECT count(*) FROM audit.decision_events "
                       "WHERE event_type='agent.grounding.failed'")).scalar()
    assert n == 3  # one grounding failure per attempt (3), then fail closed — cage held


def test_legitimate_20_sessions_in_corpus_still_grounds(clean_audit):
    s = clean_audit
    memo, _ = _run(s, _memo("Price sits above the 20 session average."),
                   evidence_body="Average over 20 sessions: 543.21 and rising.")
    assert memo.recommendation == "WATCHLIST"


def test_identifier_value_grounds_while_identifier_digits_do_not(clean_audit):
    s = clean_audit
    # the VALUE next to the identifier grounds fine (three decimals: the
    # schema's execution-number gate rejects price-shaped 2dp decimals)...
    memo, _ = _run(s, _memo("The average sits near 543.216 currently."),
                   evidence_body="SMA20 543.216 and rising.")
    assert memo.recommendation == "WATCHLIST"
    # ...and a narrative identifier asserts no numeric claim at all
    assert numeric_tokens("SMA20 crossed above SMA50") == []


def test_redteam_date_component_leak_is_dead(clean_audit):
    """'26' was substring-grounded by the '26' inside '2026-07-10'."""
    s = clean_audit
    with pytest.raises(AgentRunFailed):
        _run(s, _memo("Roughly 26 names cleared the screen."),
             evidence_body="Vendor window ending 2026-07-10.")


def test_boundary_tokenizer_units():
    assert numeric_tokens("SMA20 rising, hold 20 sessions") == ["20"]
    assert numeric_tokens("v1.2 shipped a 3.5 sigma move") == ["3.5"]
    assert numeric_tokens("priced at 20.5x earnings") == []   # never just '20'
    assert corpus_numeric_tokens(
        "SMA20 543.21 on 2026-07-10, over 20 sessions (p=0.830)") == frozenset(
        {"543.21", "2026", "07", "10", "20", "0.830"})
    # sentence-ending periods do not eat the token
    assert corpus_numeric_tokens("closed at 543.21.") == frozenset({"543.21"})


def test_grounding_violations_unit():
    memo = CommitteeMemo(recommendation="WATCHLIST", conviction="LOW",
                         thesis="Margin reached 41 percent.",
                         kill_criteria=["a", "b"], evidence_refs=["e1"],
                         dissent="none of note", evidence_available=True)
    assert grounding_violations(memo, {"e1": "margin reached 41 percent"}) == []
    assert grounding_violations(memo, {"e1": "margin expanded"}) != []
