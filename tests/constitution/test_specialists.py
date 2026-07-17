"""Specialist committee analysts (ADR-0011 step 2): the cage is tested, not
the animal.

What matters here:
1. LANE ISOLATION IS STRUCTURAL — each specialist sees and grounds against
   ONLY its lane's evidence blocks; a number imported from another lane is an
   ungrounded fabrication and the cage holds.
2. FAIL-SOFT PER SPECIALIST — a dead specialist becomes an honest absence;
   the memo still lands and the CIO context says so. This deliberately
   differs from the debate (load-bearing for the memo: the debate_summary
   contract and debate_present schema gate assume it), documented in
   roles/specialists.py. BudgetExhausted is NEVER fail-soft.
3. SIGNAL-LANE GATING — the desk runs the panel only for names whose evidence
   carries a dcp:signal: block; scanner-only names skip it (budget).
4. PROVENANCE — validated assessments persist verbatim with the memo, same
   transaction; absence means no row; no memo means no rows.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

import atlas.agents.desk as desk_mod
import atlas.agents.runtime.runner as runner_mod
from atlas.agents.desk import run_desk
from atlas.agents.roles.cio import committee_memo
from atlas.agents.roles.specialists import (
    SpecialistPanel,
    has_signal_block,
    run_specialists,
    sector_evidence,
)
from atlas.agents.runtime.budget import BudgetExhausted
from atlas.agents.runtime.llm import LlmResult, StubClient
from atlas.agents.runtime.runner import AgentRunFailed, current_budget_surface
from atlas.agents.schemas.specialist import SpecialistAssessment
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 17, 6, 0, tzinfo=UTC))


def _audit(s):
    return PostgresAuditLog(s, CLOCK)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(runner_mod, "_sleep", lambda s: None)
    monkeypatch.setenv("ATLAS_DAILY_LLM_BUDGET_USD", "10.0")
    monkeypatch.delenv("ATLAS_BUDGET_NIGHTLY", raising=False)
    monkeypatch.delenv("ATLAS_BUDGET_ANALYZE", raising=False)


# Evidence in build_evidence's real ref shapes; bodies carry standalone
# numeric tokens so grounded outputs can quote them.
BARS = ("dcp:bars:AVGO:2026-07-10",
        "AVGO daily closes: latest close 172 on 2026-07-10.")
FUND = ("dcp:fundamentals:AVGO:2026-07-08",
        "AVGO stock fundamentals (EODHD snapshot 2026-07-08): ROE 0.42, "
        "revenue growth yoy 0.16, operating margin 0.61.")
EARN = ("dcp:earnings:AVGO:2026-07-09",
        "Earnings calendar for AVGO: next scheduled report 2026-09-03 "
        "(34 sessions after 2026-07-10). Last report 2026-06-04.")
REGIME = ("dcp:regime:v1:SPY:2026-07-10",
          "Market regime (deterministic classifier v1, SPY benchmark): "
          "risk_on as of 2026-07-10.")
SIG = ("dcp:signal:xsmom:0a1b2c3d-0000-0000-0000-000000000000:2026-07-10",
       "xsmom signal: rank 2 of 12, valid through 2026-07-14.")
FULL = [BARS, FUND, EARN, REGIME, SIG]


def _assessment(stance="supportive", points=None, flags=None) -> str:
    return json.dumps({
        "stance": stance,
        "key_points": points or [
            "The cited evidence supports durable business quality",
            "Nothing in the lane contradicts the stance taken here"],
        "red_flags": flags if flags is not None else [],
        "confidence": "medium"})


def _stubs():
    return {"quality": StubClient([_assessment()]),
            "growth": StubClient([_assessment("neutral")]),
            "macro": StubClient([_assessment("concerned")])}


# --- signal-lane detection ----------------------------------------------------

def test_has_signal_block_detects_both_signal_families():
    assert has_signal_block([BARS, SIG])
    assert has_signal_block([("dcp:signal:pead:x:2026-07-10", "pead")])
    assert not has_signal_block([BARS, FUND, EARN, REGIME])
    assert not has_signal_block([])


# --- lane isolation (structural) ---------------------------------------------

def test_each_specialist_sees_only_its_lane(clean_audit):
    s = clean_audit
    stubs = _stubs()
    panel = run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                            evidence=FULL, clients=stubs)
    assert set(panel.assessments) == {"quality", "growth", "macro"}
    assert panel.absences == {}
    (qp,), (gp,), (mp,) = (stubs["quality"].prompts, stubs["growth"].prompts,
                           stubs["macro"].prompts)
    # quality: fundamentals only — no bars, no regime, no signal, no earnings
    assert FUND[1] in qp and "Quality Analyst" in qp
    for other in (BARS, EARN, REGIME, SIG):
        assert other[1] not in qp
    # growth: fundamentals + earnings, nothing else
    assert FUND[1] in gp and EARN[1] in gp and "Growth Analyst" in gp
    for other in (BARS, REGIME, SIG):
        assert other[1] not in gp
    # macro: regime + the instrument-registry sector line, nothing else
    assert REGIME[1] in mp and "GICS sector" in mp and "Macro Analyst" in mp
    for other in (BARS, FUND, EARN, SIG):
        assert other[1] not in mp
    n = s.execute(text("SELECT count(*) FROM research.agent_runs "
                       "WHERE agent_role LIKE '%_analyst' AND status='ok'")).scalar()
    assert n == 3


def test_empty_lane_means_absent_and_zero_spend(clean_audit):
    """No fundamentals in evidence: quality and growth are NOT RUN (an analyst
    with nothing to read is an invitation to fabricate) and no LLM call is
    made for them — recorded absent with the honest reason."""
    s = clean_audit
    stubs = _stubs()
    panel = run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                            evidence=[BARS, REGIME, SIG], clients=stubs)
    assert set(panel.assessments) == {"macro"}
    assert "no evidence blocks" in panel.absences["quality"]
    assert "no evidence blocks" in panel.absences["growth"]
    assert stubs["quality"].prompts == [] and stubs["growth"].prompts == []


def test_sector_evidence_reads_the_registry_and_is_honest_when_absent(clean_audit):
    s = clean_audit
    s.execute(text(
        "INSERT INTO market.instruments (symbol, exchange, market, "
        " instrument_type, sector_gics, currency) "
        "VALUES ('ZSPEC', 'TEST', 'US', 'stock', 'Information Technology', 'USD')"))
    ref, body = sector_evidence(s, "ZSPEC")
    assert ref == "dcp:instrument:ZSPEC:sector"
    assert "Information Technology" in body
    _, missing = sector_evidence(s, "NOSUCH")
    assert "no GICS sector recorded" in missing


# --- grounding red-team: the cage holds against a hostile specialist ---------

def test_fabricated_number_in_specialist_output_fails_closed(clean_audit):
    """A hostile quality analyst asserts a number that exists nowhere in its
    lane: grounding kills it, twice, and the run fails closed with the
    dedicated audit event. Specialists are advisory text — the cage is the
    control, not trust."""
    s = clean_audit
    fabricated = _assessment(points=[
        "ROE printed at 45.3 this quarter per my recollection",
        "Margins are stable per the fundamentals ref"])
    stubs = {"quality": StubClient([fabricated, fabricated]),
             "growth": StubClient([_assessment()]),
             "macro": StubClient([_assessment()])}
    panel = run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                            evidence=FULL, clients=stubs)
    assert "quality" not in panel.assessments
    assert panel.absences["quality"].startswith("cage held:")
    statuses = s.execute(text("SELECT status FROM research.agent_runs "
                              "WHERE agent_role='quality_analyst'")).scalars().all()
    assert statuses == ["schema_fail", "schema_fail"]
    n = s.execute(text("SELECT count(*) FROM audit.decision_events "
                       "WHERE event_type='agent.grounding.failed' "
                       "  AND actor_id='quality_analyst'")).scalar()
    assert n == 2


def test_number_imported_from_another_lane_is_fabrication(clean_audit):
    """Lane isolation is the grounding corpus: '34' is a REAL token in the
    earnings evidence, so the growth analyst may quote it — but the macro
    analyst quoting the very same token fails exactly like thin air, because
    its corpus is the regime lane. 'Argues only from its block' is
    structural, not rhetorical."""
    s = clean_audit
    cites_34 = _assessment(points=[
        "The calendar shows 34 sessions until the print, supporting patience",
        "Nothing in the lane contradicts the stance taken here"])
    stubs = {"quality": StubClient([_assessment()]),
             "growth": StubClient([cites_34]),           # 34 in-lane: grounded
             "macro": StubClient([cites_34, cites_34])}  # 34 cross-lane: killed
    panel = run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                            evidence=FULL, clients=stubs)
    assert "growth" in panel.assessments
    assert "macro" not in panel.assessments
    assert panel.absences["macro"].startswith("cage held:")
    n = s.execute(text("SELECT count(*) FROM audit.decision_events "
                       "WHERE event_type='agent.grounding.failed' "
                       "  AND actor_id='macro_analyst'")).scalar()
    assert n == 2


# --- fail-soft semantics ------------------------------------------------------

def test_one_dead_specialist_does_not_kill_the_memo(clean_audit):
    """Quality cage-fails; growth and macro stand; the memo still lands and
    the CIO context states the absence honestly. (The debate, by contrast, is
    load-bearing: its cage kill fails the symbol closed — see
    roles/specialists.py for why the semantics differ.)"""
    s = clean_audit
    stubs = {"quality": StubClient(["not json", "still not json"]),
             "growth": StubClient([_assessment("neutral")]),
             "macro": StubClient([_assessment("concerned")])}
    panel = run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                            evidence=FULL, clients=stubs)
    assert set(panel.assessments) == {"growth", "macro"}
    assert panel.absences["quality"].startswith("cage held:")

    cio = StubClient([json.dumps({
        "recommendation": "WATCHLIST", "conviction": "LOW",
        "thesis": "Signal present; specialist panel split and one voice absent.",
        "kill_criteria": ["Signal invalidates", "Regime flips against the sector"],
        "evidence_refs": [SIG[0]],
        "dissent": "The concerned macro stance may deserve more weight.",
        "debate_summary": ""})])
    memo = committee_memo(session=s, audit=_audit(s), client=cio, symbol="AVGO",
                          question="committee?", evidence=FULL, specialists=panel)
    assert memo.recommendation == "WATCHLIST"
    prompt = cio.prompts[0]
    assert "QUALITY analyst: NOT AVAILABLE — cage held:" in prompt
    assert "Do not infer what this specialist would have said." in prompt
    assert "GROWTH analyst: stance neutral" in prompt
    assert "MACRO analyst: stance concerned" in prompt
    # provenance: rows ONLY for the specialists the CIO actually read
    memo_id = s.execute(text("SELECT id FROM research.memos")).scalar_one()
    roles = s.execute(text("SELECT role FROM research.memo_specialists "
                           "WHERE memo_id=:m ORDER BY role"),
                      {"m": memo_id}).scalars().all()
    assert roles == ["growth", "macro"]


def test_transient_transport_death_is_an_absence_not_a_symbol_kill(clean_audit):
    s = clean_audit

    class _Dead:
        def complete(self, prompt: str, *, max_tokens: int) -> LlmResult:
            req = httpx.Request("POST", "https://api.test/v1/messages")
            raise httpx.HTTPStatusError("HTTP 503", request=req,
                                        response=httpx.Response(503, request=req))

    stubs = {"quality": _Dead(), "growth": StubClient([_assessment()]),
             "macro": StubClient([_assessment()])}
    panel = run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                            evidence=FULL, clients=stubs)
    assert set(panel.assessments) == {"growth", "macro"}
    assert panel.absences["quality"].startswith("transient:")


def test_budget_exhausted_is_never_fail_soft(clean_audit):
    """The breaker is terminal: it must PROPAGATE out of the panel (the desk
    holds the symbol and halts the shortlist), never become a quiet absence."""
    s = clean_audit
    s.execute(text(
        "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
        " model, status, cost_usd) VALUES ('seed', 'h', 'stub', 'ok', 11.0)"))
    with pytest.raises(BudgetExhausted):
        run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                        evidence=FULL, clients=_stubs())
    n = s.execute(text("SELECT count(*) FROM research.agent_runs "
                       "WHERE status='budget_kill' "
                       "  AND agent_role='quality_analyst'")).scalar()
    assert n == 1


def test_specialist_calls_bind_the_callers_budget_surface(clean_audit, monkeypatch):
    """Budget accounting: panel spend rides the shared daily tally and the
    surface watermark the caller bound — a nightly sub-cap breach kills the
    specialist run with the surface scope on the audit chain."""
    s = clean_audit
    s.execute(text(
        "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
        " model, status, cost_usd) VALUES ('seed', 'h', 'stub', 'ok', 1.0)"))
    monkeypatch.setenv("ATLAS_BUDGET_NIGHTLY", "0.50")
    from atlas.agents.runtime.runner import budget_surface
    with budget_surface("nightly"):
        with pytest.raises(BudgetExhausted):
            run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                            evidence=FULL, clients=_stubs())
    scope = s.execute(text(
        "SELECT payload->>'scope' FROM audit.decision_events "
        "WHERE event_type='cost.budget.breached'")).scalar()
    assert scope == "surface:nightly"


# --- desk wiring: signal lane only -------------------------------------------

@pytest.fixture
def desk_spy(monkeypatch):
    calls = {"specialists": [], "memo": []}
    evidence_by_symbol = {
        "SIGNM": [BARS, FUND, REGIME, SIG],          # signal lane
        "SCANO": [BARS, FUND, REGIME],               # scanner-only
    }
    sentinel = SpecialistPanel(assessments={}, absences={})

    monkeypatch.setattr(desk_mod, "build_evidence",
                        lambda s, sym: evidence_by_symbol[sym])
    monkeypatch.setattr(desk_mod, "run_debate",
                        lambda **kw: SimpleNamespace(kind="debate"))

    def fake_specialists(*, session, audit, symbol, evidence, **kw):
        calls["specialists"].append((symbol, current_budget_surface()))
        return sentinel

    def fake_memo(*, session, audit, client, symbol, question, evidence,
                  debate, specialists=None, source=None):
        calls["memo"].append((symbol, specialists))
        return SimpleNamespace(recommendation="WATCHLIST", conviction="LOW")

    monkeypatch.setattr(desk_mod, "run_specialists", fake_specialists)
    monkeypatch.setattr(desk_mod, "committee_memo", fake_memo)
    return calls, sentinel


def test_desk_runs_specialists_for_signal_lane_names_only(clean_audit, desk_spy):
    calls, sentinel = desk_spy
    report = run_desk(clean_audit, CLOCK, ["SIGNM", "SCANO"])
    assert [m.symbol for m in report.memos] == ["SIGNM", "SCANO"]
    # panel ran once, for the signal-lane name, inside the nightly surface
    assert calls["specialists"] == [("SIGNM", "nightly")]
    # the CIO got the panel for the signal name and None for scanner-only
    assert calls["memo"] == [("SIGNM", sentinel), ("SCANO", None)]


# --- persistence --------------------------------------------------------------

GOOD_MEMO = json.dumps({
    "recommendation": "WATCHLIST", "conviction": "LOW",
    "thesis": "Panel and debate weighed; quant confirmation still required.",
    "kill_criteria": ["Signal invalidates", "Regime flips against the sector"],
    "evidence_refs": [SIG[0]],
    "dissent": "The concerned macro stance may deserve more weight.",
    "debate_summary": ""})


def test_three_rows_land_with_the_memo_verbatim_same_transaction(clean_audit):
    s = clean_audit
    panel = run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                            evidence=FULL, clients=_stubs())
    committee_memo(session=s, audit=_audit(s), client=StubClient([GOOD_MEMO]),
                   symbol="AVGO", question="committee?", evidence=FULL,
                   specialists=panel)
    memo_id = s.execute(text("SELECT id FROM research.memos")).scalar_one()
    rows = s.execute(text(
        "SELECT role, payload, created_at FROM research.memo_specialists "
        "WHERE memo_id = :m"), {"m": memo_id}).all()
    assert all(r.created_at is not None for r in rows)
    # verbatim: byte-equivalent to each validated assessment (uncommitted
    # session = same transaction as the memo row)
    assert {r.role: r.payload for r in rows} == {
        role: a.model_dump() for role, a in panel.assessments.items()}
    assert {r.role for r in rows} == {"quality", "growth", "macro"}
    # and the persisted payloads round-trip through the schema
    for r in rows:
        SpecialistAssessment.model_validate(r.payload)


def test_cage_failed_memo_persists_no_specialist_rows(clean_audit):
    """Provenance can never outlive its memo: a CIO run that fails closed
    leaves no memo and therefore no specialist rows."""
    s = clean_audit
    panel = run_specialists(session=s, audit=_audit(s), symbol="AVGO",
                            evidence=FULL, clients=_stubs())
    buy_without_evidence = json.dumps({
        "recommendation": "BUY", "conviction": "HIGH",
        "thesis": "Great setup.", "kill_criteria": ["a", "b"],
        "evidence_refs": ["fake"], "dissent": "none", "debate_summary": ""})
    with pytest.raises(AgentRunFailed):
        committee_memo(session=s, audit=_audit(s),
                       client=StubClient([buy_without_evidence] * 2),
                       symbol="AVGO", question="buy?", evidence=None,
                       specialists=panel)
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() == 0
    assert s.execute(text(
        "SELECT count(*) FROM research.memo_specialists")).scalar() == 0


def test_memo_without_panel_persists_zero_specialist_rows(clean_audit):
    """An honest zero: a panel that never ran must never be fabricated."""
    s = clean_audit
    committee_memo(session=s, audit=_audit(s), client=StubClient([GOOD_MEMO]),
                   symbol="AVGO", question="committee?", evidence=FULL)
    assert s.execute(text("SELECT count(*) FROM research.memos")).scalar() == 1
    assert s.execute(text(
        "SELECT count(*) FROM research.memo_specialists")).scalar() == 0
