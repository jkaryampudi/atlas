"""Red-team + happy-path for scanner/research/macro/sector roles."""
import json
from datetime import UTC, datetime

import pytest

from atlas.agents.roles.committee import (macro_regime, research_memo,
                                          scanner_shortlist, sector_note)
from atlas.agents.runtime.llm import StubClient
from atlas.agents.runtime.runner import AgentRunFailed
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg


def _audit(s):
    return PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 11, 7, 0, tzinfo=UTC)))


CANDS = [("AVGO", "sig:1", "momentum long"), ("MSFT", "sig:2", "momentum long"),
         ("INDA", "sig:3", "regime long")]


def test_scanner_cannot_invent_candidates(clean_audit):
    s = clean_audit
    rogue = json.dumps({"shortlist": [
        {"symbol": "GME", "signal_ref": "sig:x", "rationale": "vibes"}],
        "excluded_count": 0})
    with pytest.raises(AgentRunFailed):
        scanner_shortlist(session=s, audit=_audit(s),
                          client=StubClient([rogue, rogue]),
                          candidates=CANDS, max_candidates=2)


def test_scanner_cannot_exceed_funnel_cap(clean_audit):
    s = clean_audit
    over = json.dumps({"shortlist": [
        {"symbol": sym, "signal_ref": ref, "rationale": "fits"} for sym, ref, _ in CANDS],
        "excluded_count": 0})
    with pytest.raises(AgentRunFailed):
        scanner_shortlist(session=s, audit=_audit(s), client=StubClient([over, over]),
                          candidates=CANDS, max_candidates=2)


def test_scanner_happy_path(clean_audit):
    s = clean_audit
    ok = json.dumps({"shortlist": [
        {"symbol": "AVGO", "signal_ref": "sig:1", "rationale": "strongest trend structure"}],
        "excluded_count": 2})
    out = scanner_shortlist(session=s, audit=_audit(s), client=StubClient([ok]),
                            candidates=CANDS, max_candidates=2)
    assert out.shortlist[0].symbol == "AVGO"


def test_research_buy_gate_matches_cio_rules(clean_audit):
    s = clean_audit
    rogue = json.dumps({"recommendation": "BUY", "conviction": "HIGH",
                        "thesis": "great", "business_quality": "STRONG", "moat": "WIDE",
                        "kill_criteria": ["a", "b"], "evidence_refs": ["fake"],
                        "dissent": "none"})
    with pytest.raises(AgentRunFailed):
        research_memo(session=s, audit=_audit(s), client=StubClient([rogue, rogue]),
                      symbol="AVGO", evidence=[])


def test_macro_enum_and_numeric_guard(clean_audit):
    s = clean_audit
    numeric = json.dumps({"us_regime": "RISK_ON", "india_regime": "NEUTRAL",
                          "summary": "Cut rates to 3.5% means +12% upside for banks.",
                          "sector_tags": [], "evidence_refs": ["m:1"], "dissent": "x"})
    with pytest.raises(AgentRunFailed):
        macro_regime(session=s, audit=_audit(s), client=StubClient([numeric, numeric]),
                     evidence=[("m:1", "policy statement digest")])


def test_sector_note_happy_path(clean_audit):
    s = clean_audit
    ok = json.dumps({"sector_view": "MIXED",
                     "context": "Custom silicon demand strong; customer concentration is the watch item.",
                     "red_flags": ["hyperscaler capex dependence"], "evidence_refs": ["e:1"]})
    out = sector_note(session=s, audit=_audit(s), client=StubClient([ok]),
                      sector="Information Technology", symbol="AVGO",
                      evidence=[("e:1", "sector digest")])
    assert out.sector_view == "MIXED"
