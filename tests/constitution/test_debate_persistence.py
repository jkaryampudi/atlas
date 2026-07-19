"""Debate provenance (migration 0019, desk-review 2026-07 item 7):
committee_memo persists the four validated DebateCases VERBATIM with the memo
they informed — same transaction, same discipline as memo_evidence — and
persists NOTHING when the cage fails the run closed or when no debate ran.
This unlocks diversity measurement (are bull and bear anchored copies?);
nothing here measures anything."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from atlas.agents.roles.cio import committee_memo
from atlas.agents.roles.debate import DebateResult
from atlas.agents.runtime.llm import StubClient
from atlas.agents.runtime.runner import SCHEMA_MAX_ATTEMPTS, AgentRunFailed
from atlas.agents.schemas.debate import DebateCase
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg


def _audit(s):
    return PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 13, 6, 0, tzinfo=UTC)))


EVIDENCE = [("sig-1", "momentum signal snapshot: trend intact per DCP output sig-1")]


def _case(stance: str, first_point: str) -> DebateCase:
    return DebateCase(
        stance=stance, expected_stance=stance,
        strongest_points=[first_point,
                          "Hyperscaler intentions support multi-year visibility",
                          "Networking share gains corroborated by the cited signal"],
        weakest_opposing_point="Concentration in a handful of buyers is fragile.",
        evidence_refs=["sig-1"],
        concede="The thesis depends on capex cycles that mean-revert.")


def _debate() -> DebateResult:
    # four DISTINCT cases so verbatim persistence is provable per seat
    return DebateResult(bull=_case("BULL", "Bull opening argument"),
                        bear=_case("BEAR", "Bear opening argument"),
                        bull_rebuttal=_case("BULL", "Bull rebuttal argument"),
                        bear_rebuttal=_case("BEAR", "Bear rebuttal argument"))


GOOD = json.dumps({
    "recommendation": "WATCHLIST", "conviction": "LOW",
    "thesis": "Debate sharpened the case; quant confirmation still required.",
    "kill_criteria": ["Capex guidance turns negative", "Custom-ASIC customer loss"],
    "evidence_refs": ["sig-1"],
    "dissent": "The bear case on customer concentration stands.",
    "debate_summary": "Sides disagree on durability of hyperscaler demand."})


def test_four_debate_rows_land_with_the_memo_verbatim(clean_audit):
    s = clean_audit
    debate = _debate()
    committee_memo(session=s, audit=_audit(s), client=StubClient([GOOD]),
                   symbol="AVGO", question="committee decision?",
                   evidence=EVIDENCE, debate=debate)
    memo_id = s.execute(text("SELECT id FROM research.memos "
                             "WHERE instrument_symbol = 'AVGO'")).scalar_one()
    rows = s.execute(text(
        "SELECT role, payload, created_at FROM research.memo_debate "
        "WHERE memo_id = :m"), {"m": memo_id}).all()
    assert all(r.created_at is not None for r in rows)
    # verbatim: the persisted JSON is byte-equivalent to the validated case
    assert {r.role: r.payload for r in rows} == {
        "bull": debate.bull.model_dump(),
        "bear": debate.bear.model_dump(),
        "bull_rebuttal": debate.bull_rebuttal.model_dump(),
        "bear_rebuttal": debate.bear_rebuttal.model_dump()}


def test_cage_fail_persists_no_memo_and_no_debate_rows(clean_audit):
    """Fail-closed stays whole: a killed run leaves no memo, so it must leave
    no debate rows either — provenance can never outlive its memo."""
    s = clean_audit
    buy_without_evidence = json.dumps({
        "recommendation": "BUY", "conviction": "HIGH",
        "thesis": "Great setup.", "kill_criteria": ["a", "b"],
        "evidence_refs": ["fake"], "dissent": "none",
        "debate_summary": "unanimous"})
    with pytest.raises(AgentRunFailed):
        committee_memo(session=s, audit=_audit(s),
                       client=StubClient([buy_without_evidence] * SCHEMA_MAX_ATTEMPTS),
                       symbol="AVGO", question="buy?", evidence=None,
                       debate=_debate())
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() == 0
    assert s.execute(text("SELECT count(*) FROM research.memo_debate")).scalar() == 0


def test_memo_without_debate_persists_zero_debate_rows(clean_audit):
    """An honest zero: a debate that never ran must never be fabricated."""
    s = clean_audit
    no_debate = json.dumps({
        "recommendation": "WATCHLIST", "conviction": "LOW",
        "thesis": "Trend evidence present; no debate was convened.",
        "kill_criteria": ["Trend breaks", "Gates keep failing"],
        "evidence_refs": ["sig-1"], "dissent": "Unvalidated family."})
    committee_memo(session=s, audit=_audit(s), client=StubClient([no_debate]),
                   symbol="AVGO", question="what now?", evidence=EVIDENCE)
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() == 1
    assert s.execute(text("SELECT count(*) FROM research.memo_debate")).scalar() == 0
