"""Desk-loop failure semantics (desk-review 2026-07 item 6): TRANSIENT
transport failures back off in the runner and become per-symbol skips; CAGE
verdicts keep today's one-retry-then-fail-closed behavior, now with the
violation text appended via the reviewed retry template; BudgetExhausted is a
cage hold that halts the shortlist, never a crash. The cage is tested, not
the animal: misbehaving transports and models are scripted, and the desk must
stay honest around them."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import text

import atlas.agents.desk as desk_mod
import atlas.agents.runtime.runner as runner_mod
from atlas.agents.desk import run_desk
from atlas.agents.runtime.budget import BudgetExhausted
from atlas.agents.runtime.llm import LlmResult
from atlas.agents.runtime.runner import (
    AgentRunFailed,
    TransientLlmFailure,
    budget_surface,
    current_budget_surface,
    load_retry_template,
    run_agent,
)
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 13, 6, 0, tzinfo=UTC))


def _audit(s):
    return PostgresAuditLog(s, CLOCK)


def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.test/v1/messages")
    return httpx.HTTPStatusError(f"HTTP {code}", request=req,
                                 response=httpx.Response(code, request=req))


class _Note(BaseModel):
    note: str


class _Grounded(BaseModel):
    note: str
    evidence_refs: list[str]


class _FlakyStub:
    """Scripted exceptions first, then scripted responses — records prompts."""

    def __init__(self, failures, responses):
        self._failures = list(failures)
        self._responses = list(responses)
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, prompt: str, *, max_tokens: int) -> LlmResult:
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        self.prompts.append(prompt)
        text_ = self._responses.pop(0)
        return LlmResult(text=text_, tokens_in=10, tokens_out=5, model="stub")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(runner_mod, "_sleep", lambda s: None)
    monkeypatch.setenv("ATLAS_DAILY_LLM_BUDGET_USD", "10.0")
    monkeypatch.delenv("ATLAS_BUDGET_NIGHTLY", raising=False)
    monkeypatch.delenv("ATLAS_BUDGET_ANALYZE", raising=False)


# --- runner level -------------------------------------------------------------

def test_transient_failures_backoff_then_run_succeeds(clean_audit):
    s = clean_audit
    client = _FlakyStub([_http_error(429), _http_error(503)],
                        [json.dumps({"note": "ok"})])
    out, _ = run_agent(session=s, audit=_audit(s), client=client,
                       agent_role="probe", template_rel_path="debate/bull.md",
                       context="probe", output_model=_Note, input_refs=[])
    assert out.note == "ok" and client.calls == 3
    status = s.execute(text("SELECT status FROM research.agent_runs")).scalar()
    assert status == "ok"                       # the recovered run is a normal run


def test_exhausted_transient_raises_and_persists_no_run(clean_audit):
    s = clean_audit
    client = _FlakyStub([_http_error(503)] * 10, [])
    with pytest.raises(TransientLlmFailure):
        run_agent(session=s, audit=_audit(s), client=client,
                  agent_role="probe", template_rel_path="debate/bull.md",
                  context="probe", output_model=_Note, input_refs=[])
    # never completed => no usage data => nothing to bill or record
    assert s.execute(text("SELECT count(*) FROM research.agent_runs")).scalar() == 0


def test_cage_kill_is_never_retried_as_transient(clean_audit):
    """A schema kill takes the cage path (SCHEMA_MAX_ATTEMPTS=3, then fail
    closed) — the transient backoff must not multiply cage retries beyond that."""
    s = clean_audit
    client = _FlakyStub([], ["not json at all", "still not json", "still bad"])
    with pytest.raises(AgentRunFailed):
        run_agent(session=s, audit=_audit(s), client=client,
                  agent_role="probe", template_rel_path="debate/bull.md",
                  context="probe", output_model=_Note, input_refs=[])
    assert client.calls == 3                    # exactly SCHEMA_MAX_ATTEMPTS, no more
    statuses = s.execute(text(
        "SELECT status FROM research.agent_runs")).scalars().all()
    assert statuses == ["schema_fail", "schema_fail", "schema_fail"]


def test_cage_retry_appends_violation_text_via_reviewed_template(clean_audit):
    s = clean_audit
    bad = json.dumps({"note": "the price is 123.45", "evidence_refs": ["e1"]})
    good = json.dumps({"note": "grounded and digit-free", "evidence_refs": ["e1"]})
    client = _FlakyStub([], [bad, good])
    out, _ = run_agent(session=s, audit=_audit(s), client=client,
                       agent_role="probe", template_rel_path="debate/bull.md",
                       context="probe", output_model=_Grounded,
                       input_refs=[{"type": "evidence", "id": "e1"}],
                       evidence_bodies={"e1": "trend evidence with no numerals"})
    assert out.note == "grounded and digit-free"
    first, second = client.prompts
    retry_template, retry_hash = load_retry_template()
    assert "Retry Addendum" not in first                     # attempt 1 is clean
    assert "Retry Addendum — Prior Attempt Rejected" in second
    assert "ungrounded number '123.45'" in second            # violation text rode along
    assert "{violations}" not in second                      # substitution happened
    # the reviewed template hash is pinned on the audit chain for attempt 2
    payloads = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'agent.run.completed' ORDER BY seq")).scalars().all()
    assert "retry_template_hash" not in payloads[0]
    assert payloads[1]["retry_template_hash"] == retry_hash[:12]


# --- desk level ----------------------------------------------------------------

EVIDENCE = [("sig-1", "trend intact per DCP output sig-1")]


@pytest.fixture
def desk_stubs(monkeypatch):
    """Stub the desk's collaborators; scripts per-symbol debate behavior."""
    behaviors: dict[str, BaseException | None] = {}

    def fake_build_evidence(session, symbol):
        return EVIDENCE

    def fake_run_debate(*, session, audit, symbol, evidence, **kw):
        exc = behaviors.get(symbol)
        if exc is not None:
            raise exc
        return SimpleNamespace(kind="debate", symbol=symbol)

    def fake_committee_memo(*, session, audit, client, symbol, question,
                            evidence, debate, specialists=None, source=None):
        # specialists (ADR-0011 step 2) is None here: the stub evidence has no
        # dcp:signal: block, so the desk's signal-lane gate skips the panel
        return SimpleNamespace(recommendation="WATCHLIST", conviction="LOW")

    monkeypatch.setattr(desk_mod, "build_evidence", fake_build_evidence)
    monkeypatch.setattr(desk_mod, "run_debate", fake_run_debate)
    monkeypatch.setattr(desk_mod, "committee_memo", fake_committee_memo)
    return behaviors


def test_shortlist_survives_one_symbols_transient_death(clean_audit, desk_stubs):
    desk_stubs["AAA"] = TransientLlmFailure(
        "transient LLM failure after 3 attempts: HTTPStatusError: HTTP 503")
    report = run_desk(clean_audit, CLOCK, ["AAA", "BBB"])
    assert [m.symbol for m in report.memos] == ["BBB"]       # the desk kept going
    assert report.cage_holds == ()                           # plumbing is not a verdict
    (sym, why), = report.skipped
    assert sym == "AAA" and why.startswith("transient: ")
    assert "HTTP 503" in why


def test_cage_hold_still_recorded_per_symbol_and_loop_continues(clean_audit,
                                                                desk_stubs):
    desk_stubs["AAA"] = AgentRunFailed("probe: two consecutive schema failures")
    report = run_desk(clean_audit, CLOCK, ["AAA", "BBB"])
    assert [m.symbol for m in report.memos] == ["BBB"]
    (sym, why), = report.cage_holds
    assert sym == "AAA" and "schema failures" in why
    assert report.skipped == ()


def test_budget_exhausted_becomes_cage_holds_and_halts_the_shortlist(
        clean_audit, desk_stubs):
    """A tripped breaker is terminal for the run: the symbol holds, the rest
    are honest not-attempted holds (attempting them would spend at the vendor
    before the check), and nothing crashes."""
    desk_stubs["BBB"] = BudgetExhausted("daily LLM budget breached: 10.01 > 10.00 USD")
    report = run_desk(clean_audit, CLOCK, ["AAA", "BBB", "CCC", "DDD"])
    assert [m.symbol for m in report.memos] == ["AAA"]       # work before the trip stands
    assert report.cage_holds == (
        ("BBB", "budget: daily LLM budget breached: 10.01 > 10.00 USD"),
        ("CCC", "budget exhausted — not attempted"),
        ("DDD", "budget exhausted — not attempted"))


def test_desk_binds_the_nightly_surface_unless_caller_bound_one(clean_audit,
                                                                monkeypatch):
    seen: list[str | None] = []

    def spying_debate(*, session, audit, symbol, evidence, **kw):
        seen.append(current_budget_surface())
        raise AgentRunFailed("stop here")

    monkeypatch.setattr(desk_mod, "build_evidence", lambda s, sym: EVIDENCE)
    monkeypatch.setattr(desk_mod, "run_debate", spying_debate)
    run_desk(clean_audit, CLOCK, ["AAA"])                    # bare call = nightly
    with budget_surface("analyze"):                          # analyze.py's binding
        run_desk(clean_audit, CLOCK, ["AAA"])
    assert seen == ["nightly", "analyze"]
