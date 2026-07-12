"""Evidence provenance (migration 0013): committee_memo persists the EXACT
(ref, body) pairs the agents read, verbatim and in input order, with the memo
they produced — and persists NOTHING when the cage fails the run closed.

This is the desk path's persistence seam: desk.py and live_run.py both reach
memo persistence only through committee_memo, so proving it here proves both.
"""
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from atlas.agents.roles.cio import committee_memo
from atlas.agents.runtime.llm import StubClient
from atlas.agents.runtime.runner import AgentRunFailed
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg


def _audit(s):
    return PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 11, 6, 0, tzinfo=UTC)))


# deliberately NON-alphabetical refs: ordinal order must be INPUT order,
# never an accidental sort
EVIDENCE = [
    ("quant:report:momentum-v1:AVGO", "momentum v1 failed every gate on real data"),
    ("dcp:bars:AVGO:2026-07-10", "vendor closes window ending at the cited date"),
    ("dcp:indicators:AVGO:2026-07-10", "trend indicators computed by the DCP"),
    ("dcp:fundamentals:AVGO:2026-07-10", "whitelisted numeric fundamentals facts"),
]

GOOD = json.dumps({
    "recommendation": "WATCHLIST", "conviction": "LOW",
    "thesis": "Trend evidence is present but no validated strategy covers the name.",
    "kill_criteria": ["Trend structure breaks down", "Quant gates keep failing"],
    "evidence_refs": ["dcp:bars:AVGO:2026-07-10"],
    "dissent": "The quant report argues the whole family is unvalidated."})


def test_memo_evidence_rows_persist_in_input_order(clean_audit):
    s = clean_audit
    committee_memo(session=s, audit=_audit(s), client=StubClient([GOOD]),
                   symbol="AVGO", question="what now?", evidence=EVIDENCE)
    rows = s.execute(text(
        "SELECT me.ordinal, me.ref, me.body FROM research.memo_evidence me "
        "JOIN research.memos m ON m.id = me.memo_id "
        "WHERE m.instrument_symbol = 'AVGO' ORDER BY me.ordinal")).all()
    assert [(r.ordinal, r.ref, r.body) for r in rows] == [
        (i, ref, body) for i, (ref, body) in enumerate(EVIDENCE)]


def test_cage_fail_persists_no_memo_and_no_evidence(clean_audit):
    """Fail-closed stays whole: no memo row means no evidence rows either."""
    s = clean_audit
    buy_without_evidence = json.dumps({
        "recommendation": "BUY", "conviction": "HIGH",
        "thesis": "Great setup.", "kill_criteria": ["a", "b"],
        "evidence_refs": ["fake"], "dissent": "none"})
    with pytest.raises(AgentRunFailed):
        committee_memo(session=s, audit=_audit(s),
                       client=StubClient([buy_without_evidence, buy_without_evidence]),
                       symbol="AVGO", question="buy?", evidence=None)
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() == 0
    assert s.execute(text("SELECT count(*) FROM research.memo_evidence")).scalar() == 0


def test_memo_without_evidence_persists_memo_and_zero_rows(clean_audit):
    """An INSUFFICIENT_EVIDENCE memo with no evidence set is honest: the memo
    lands, the provenance table records exactly nothing — never a fabricated
    placeholder row."""
    s = clean_audit
    none_given = json.dumps({
        "recommendation": "INSUFFICIENT_EVIDENCE", "conviction": "N/A",
        "thesis": "No DCP evidence was provided for this candidate.",
        "kill_criteria": [], "evidence_refs": [], "dissent": ""})
    committee_memo(session=s, audit=_audit(s), client=StubClient([none_given]),
                   symbol="AVGO", question="what now?", evidence=None)
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() == 1
    assert s.execute(text("SELECT count(*) FROM research.memo_evidence")).scalar() == 0
