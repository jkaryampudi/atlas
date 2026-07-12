"""Per-surface budget sub-caps (desk-review 2026-07 item 6): the global
$10/day breaker stays constitutional and ALWAYS wins; ATLAS_BUDGET_ANALYZE /
ATLAS_BUDGET_NIGHTLY are stricter watermarks on the same shared daily tally,
bound where each surface enters the runner — so an analyze spree can never
starve the nightly desk. Surface kills persist a budget_kill row and an audit
event with the breached scope, exactly like global kills."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import text

import atlas.agents.desk as desk_mod
from atlas.agents.desk import run_desk
from atlas.agents.runtime.budget import BudgetExhausted
from atlas.agents.runtime.llm import StubClient
from atlas.agents.runtime.runner import budget_surface, run_agent
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg

CLOCK = FrozenClock(datetime(2026, 7, 13, 6, 0, tzinfo=UTC))


class _Note(BaseModel):
    note: str


GOOD = json.dumps({"note": "ok"})


@pytest.fixture(autouse=True)
def _budget_env(monkeypatch):
    monkeypatch.setenv("ATLAS_DAILY_LLM_BUDGET_USD", "10.0")
    monkeypatch.delenv("ATLAS_BUDGET_NIGHTLY", raising=False)
    monkeypatch.delenv("ATLAS_BUDGET_ANALYZE", raising=False)


def _audit(s):
    return PostgresAuditLog(s, CLOCK)


def _seed_spend(s, usd: float) -> None:
    """Prior spend today on the shared tally (created_at defaults to now())."""
    s.execute(text(
        "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
        " model, status, cost_usd) VALUES ('seed', 'h', 'stub', 'ok', :c)"),
        {"c": usd})


def _run(s, surface: str | None = None):
    def call():
        return run_agent(session=s, audit=_audit(s), client=StubClient([GOOD]),
                         agent_role="subcap_probe",
                         template_rel_path="debate/bull.md", context="probe",
                         output_model=_Note, input_refs=[])
    if surface is None:
        return call()
    with budget_surface(surface):
        return call()


def _kill_rows(s) -> int:
    return s.execute(text("SELECT count(*) FROM research.agent_runs "
                          "WHERE status = 'budget_kill'")).scalar()


def _breach_scope(s) -> str:
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'cost.budget.breached'")).scalar()
    return payload["scope"]


def test_analyze_blocked_at_its_cap_while_nightly_proceeds(clean_audit):
    s = clean_audit
    _seed_spend(s, 3.50)                       # past analyze's 3.00 watermark
    with pytest.raises(BudgetExhausted, match=r"surface budget breached \(analyze\)"):
        _run(s, "analyze")
    assert _kill_rows(s) == 1                  # the kill is recorded, not lost
    assert _breach_scope(s) == "surface:analyze"
    out, _ = _run(s, "nightly")                # 3.5x < 6.00: nightly is untouched
    assert out.note == "ok"


def test_global_breaker_always_wins_over_a_generous_subcap(clean_audit, monkeypatch):
    s = clean_audit
    monkeypatch.setenv("ATLAS_BUDGET_NIGHTLY", "99.0")   # misconfigured wide open
    _seed_spend(s, 10.50)
    with pytest.raises(BudgetExhausted, match="daily LLM budget breached"):
        _run(s, "nightly")
    assert _breach_scope(s) == "global"        # the constitutional cap fired first


def test_unbound_entry_points_answer_to_the_global_cap_alone(clean_audit):
    s = clean_audit
    _seed_spend(s, 7.00)                       # above every sub-cap default
    out, _ = _run(s, surface=None)             # e.g. manual live_run
    assert out.note == "ok"


def test_env_override_tightens_a_surface(clean_audit, monkeypatch):
    s = clean_audit
    monkeypatch.setenv("ATLAS_BUDGET_ANALYZE", "0.50")
    _seed_spend(s, 0.60)
    with pytest.raises(BudgetExhausted, match=r"0.50 USD sub-cap"):
        _run(s, "analyze")


def test_desk_surfaces_end_to_end_analyze_holds_nightly_lands_memos(
        clean_audit, monkeypatch):
    """Through the REAL desk loop: with the day's tally past the analyze
    watermark, an analyze-bound desk records budget cage holds while the bare
    (nightly) desk still lands its memo."""
    s = clean_audit
    _seed_spend(s, 3.50)

    def fake_build_evidence(session, symbol):
        return [("sig-1", "trend intact per DCP output sig-1")]

    def real_runner_debate(*, session, audit, symbol, evidence, **kw):
        # one real cage call so the sub-cap check actually fires
        run_agent(session=session, audit=audit, client=StubClient([GOOD]),
                  agent_role="debate_bull", template_rel_path="debate/bull.md",
                  context=symbol, output_model=_Note, input_refs=[])
        return SimpleNamespace(kind="debate")

    def fake_committee_memo(**kw):
        return SimpleNamespace(recommendation="WATCHLIST", conviction="LOW")

    monkeypatch.setattr(desk_mod, "build_evidence", fake_build_evidence)
    monkeypatch.setattr(desk_mod, "run_debate", real_runner_debate)
    monkeypatch.setattr(desk_mod, "committee_memo", fake_committee_memo)

    with budget_surface("analyze"):            # exactly analyze.py's binding
        blocked = run_desk(s, CLOCK, ["AAA", "BBB"])
    assert blocked.memos == ()
    assert [h for h in blocked.cage_holds] == [
        ("AAA", blocked.cage_holds[0][1]),
        ("BBB", "budget exhausted — not attempted")]
    assert "surface budget breached (analyze)" in blocked.cage_holds[0][1]

    proceeded = run_desk(s, CLOCK, ["CCC"])    # bare call binds 'nightly'
    assert [(m.symbol, m.recommendation) for m in proceeded.memos] == [
        ("CCC", "WATCHLIST")]
    assert proceeded.cage_holds == ()
