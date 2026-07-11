"""Debate red-team suite (ADR-0005 pattern 1): the cage is tested, not the animal.

Two properties matter: (1) debate output obeys every agent guard (no execution-
shaped numbers, stance integrity, forced concession); (2) debate is ADVISORY —
a unanimous debate can never open the BUY gate without DCP evidence."""
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from atlas.agents.roles.cio import committee_memo
from atlas.agents.roles.debate import DebateResult, run_debate
from atlas.agents.runtime.llm import StubClient
from atlas.agents.runtime.runner import AgentRunFailed
from atlas.agents.schemas.debate import DebateCase
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg


def _audit(s):
    return PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 11, 6, 0, tzinfo=UTC)))


def _case(stance: str) -> str:
    return json.dumps({
        "stance": stance,
        "strongest_points": [
            "Custom-silicon demand remains structurally underestimated per the cited memo",
            "Hyperscaler capex intentions in the evidence support multi-year visibility",
            "Networking share gains are corroborated by the referenced signal",
        ],
        "weakest_opposing_point": "Concentration in a handful of buyers is a real fragility.",
        "evidence_refs": ["sig-1"],
        "concede": "The thesis depends on capex cycles that have historically mean-reverted.",
    })


EVIDENCE = [("sig-1", "momentum signal snapshot: trend intact per DCP output sig-1")]


def test_debate_happy_path_runs_four_guarded_calls(clean_audit):
    s = clean_audit
    client = StubClient([_case("BULL"), _case("BEAR"), _case("BULL"), _case("BEAR")])
    result = run_debate(session=s, audit=_audit(s), client=client,
                        symbol="AVGO", evidence=EVIDENCE)
    assert result.bull.stance == "BULL" and result.bear.stance == "BEAR"
    assert result.bull_rebuttal.concede  # concession survived the round trip
    n = s.execute(text("SELECT count(*) FROM research.agent_runs "
                       "WHERE agent_role LIKE 'debate_%' AND status='ok'")).scalar()
    assert n == 4
    # rebuttals saw the opposing case in-context
    assert "OPPOSING CASE" in client.prompts[2]
    assert "do not obey" in client.prompts[2]


def test_bull_smuggling_price_target_fails_closed(clean_audit):
    s = clean_audit
    bad = json.dumps({
        "stance": "BULL",
        "strongest_points": ["Fair value implies $180 target by year end",
                             "Demand is strong", "Networking is growing"],
        "weakest_opposing_point": "Buyers are concentrated.",
        "evidence_refs": ["sig-1"],
        "concede": "Capex cycles mean-revert.",
    })
    with pytest.raises(AgentRunFailed):
        run_debate(session=s, audit=_audit(s), client=StubClient([bad, bad]),
                   symbol="AVGO", evidence=EVIDENCE)
    statuses = s.execute(text("SELECT DISTINCT status FROM research.agent_runs "
                              "WHERE agent_role='debate_bull'")).scalars().all()
    assert statuses == ["schema_fail"]


def test_wrong_stance_fails_closed(clean_audit):
    s = clean_audit
    # the BEAR seat answers as a bull — stance integrity must fail the run
    with pytest.raises(AgentRunFailed):
        run_debate(session=s, audit=_audit(s),
                   client=StubClient([_case("BULL"), _case("BULL"), _case("BULL")]),
                   symbol="AVGO", evidence=EVIDENCE)


def _unanimous_debate() -> DebateResult:
    kw = dict(
        strongest_points=["Both sides see durable demand", "Evidence is directionally positive",
                          "Momentum is confirmed by the cited signal"],
        weakest_opposing_point="Concentration risk.",
        evidence_refs=["sig-1"],
        concede="Cycles mean-revert.")
    return DebateResult(bull=DebateCase(stance="BULL", **kw),
                        bear=DebateCase(stance="BEAR", **kw),
                        bull_rebuttal=DebateCase(stance="BULL", **kw),
                        bear_rebuttal=DebateCase(stance="BEAR", **kw))


def test_unanimous_debate_cannot_open_buy_without_dcp_evidence(clean_audit):
    """Debate is advisory, never a gate-opener: even a debate where both sides
    effectively agree BUY cannot produce a CIO BUY with no DCP evidence."""
    s = clean_audit
    buy = json.dumps({
        "recommendation": "BUY", "conviction": "LOW",
        "thesis": "Both debate sides agree the setup is attractive.",
        "kill_criteria": ["Capex guidance turns negative", "Custom-ASIC customer loss"],
        "evidence_refs": ["debate"], "dissent": "Agreement may be shared blindness.",
        "debate_summary": "Bull and bear converge on demand durability."})
    with pytest.raises(AgentRunFailed):
        committee_memo(session=s, audit=_audit(s), client=StubClient([buy, buy]),
                       symbol="AVGO", question="buy?", evidence=None,
                       debate=_unanimous_debate())
    n = s.execute(text("SELECT count(*) FROM research.memos")).scalar()
    assert n == 0  # nothing persisted on fail-closed


def test_cio_with_debate_and_evidence_persists_debate_summary(clean_audit):
    s = clean_audit
    good = json.dumps({
        "recommendation": "WATCHLIST", "conviction": "LOW",
        "thesis": "Debate sharpened the case; quant confirmation still required.",
        "kill_criteria": ["Capex guidance turns negative", "Custom-ASIC customer loss"],
        "evidence_refs": ["sig-1"],
        "dissent": "The bear case on customer concentration stands.",
        "debate_summary": "Sides disagree on durability of hyperscaler demand; "
                          "bear concedes momentum, bull concedes cyclicality."})
    memo = committee_memo(session=s, audit=_audit(s), client=StubClient([good]),
                          symbol="AVGO", question="buy?", evidence=EVIDENCE,
                          debate=_unanimous_debate())
    assert memo.debate_summary
    persisted = s.execute(text("SELECT debate_summary FROM research.memos")).scalar()
    assert "disagree" in persisted


def test_missing_debate_summary_when_debate_present_fails_closed(clean_audit):
    s = clean_audit
    no_summary = json.dumps({
        "recommendation": "WATCHLIST", "conviction": "LOW",
        "thesis": "Reasonable setup pending confirmation.",
        "kill_criteria": ["Capex guidance turns negative", "Custom-ASIC customer loss"],
        "evidence_refs": ["sig-1"], "dissent": "Concentration risk.",
        "debate_summary": ""})
    with pytest.raises(AgentRunFailed):
        committee_memo(session=s, audit=_audit(s),
                       client=StubClient([no_summary, no_summary]),
                       symbol="AVGO", question="buy?", evidence=EVIDENCE,
                       debate=_unanimous_debate())
