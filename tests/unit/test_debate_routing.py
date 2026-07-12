"""Per-side debate model routing (desk-review 2026-07 item 7): run_debate no
longer funnels all four calls through one debate_bull client — each seat gets
its own registry client, so ATLAS_MODEL_DEBATE_BEAR can fire and the local/
3090 route works per side. Pure unit: run_agent is faked, no Postgres, no
network (client CONSTRUCTION is exercised; nothing is called)."""
from __future__ import annotations

import pytest

import atlas.agents.roles.debate as debate_mod
from atlas.agents.roles.debate import run_debate
from atlas.agents.runtime.llm import AnthropicClient, OpenAICompatClient
from atlas.agents.schemas.debate import DebateCase


def _case(side: str) -> DebateCase:
    return DebateCase(
        stance=side,
        strongest_points=["Demand is durable per the cited evidence",
                          "Trend is intact per the cited signal",
                          "Margins hold per the referenced memo"],
        weakest_opposing_point="Buyer concentration is a real fragility.",
        evidence_refs=["sig-1"],
        concede="Capex cycles mean-revert.")


@pytest.fixture
def seen(monkeypatch):
    """Fake run_agent: records the client and side of each call."""
    calls: list[tuple[object, str]] = []

    def fake_run_agent(*, client, extra_fields, **kw):
        side = extra_fields["expected_stance"]
        calls.append((client, side))
        return _case(side), "run-id"

    monkeypatch.setattr(debate_mod, "run_agent", fake_run_agent)
    return calls


EVIDENCE = [("sig-1", "trend intact per DCP output sig-1")]


def test_each_side_builds_its_own_registry_client(seen, monkeypatch):
    built: list[str] = []

    def fake_build_client(role):
        built.append(role)
        return f"client-for-{role}"

    monkeypatch.setattr(debate_mod, "build_client", fake_build_client)
    run_debate(session=None, audit=None, symbol="AVGO", evidence=EVIDENCE)
    assert built == ["debate_bull", "debate_bear"]  # one client per seat
    # bull case + bull rebuttal on the bull client; bear likewise
    assert [(c, s) for c, s in seen] == [
        ("client-for-debate_bull", "BULL"),
        ("client-for-debate_bear", "BEAR"),
        ("client-for-debate_bull", "BULL"),
        ("client-for-debate_bear", "BEAR")]


def test_legacy_shared_client_override_still_works(seen, monkeypatch):
    """live_run.py still passes client= — all four calls on that one client,
    byte-for-byte the old behavior, and the registry is never consulted."""
    def boom(role):  # pragma: no cover - failing is the assertion
        raise AssertionError("registry must not be consulted")

    monkeypatch.setattr(debate_mod, "build_client", boom)
    shared = object()
    run_debate(session=None, audit=None, client=shared, symbol="AVGO",
               evidence=EVIDENCE)
    assert [c for c, _ in seen] == [shared] * 4


def test_explicit_per_side_clients_win_over_shared(seen, monkeypatch):
    monkeypatch.setattr(debate_mod, "build_client",
                        lambda role: pytest.fail("registry must not be consulted"))
    b1, b2, shared = object(), object(), object()
    run_debate(session=None, audit=None, client=shared, symbol="AVGO",
               evidence=EVIDENCE, bull_client=b1, bear_client=b2)
    assert [c for c, _ in seen] == [b1, b2, b1, b2]


def test_registry_env_routes_bear_to_local_while_bull_stays_anthropic(
        seen, monkeypatch):
    """The dead-config fix pinned end-to-end through the REAL registry:
    ATLAS_MODEL_DEBATE_BEAR=local/... routes the bear seat to the LAN
    OpenAI-compatible client while the bull seat stays on Anthropic."""
    monkeypatch.delenv("ATLAS_MODEL_DEBATE_BULL", raising=False)
    monkeypatch.delenv("ATLAS_MODEL_DEFAULT", raising=False)
    monkeypatch.setenv("ATLAS_MODEL_DEBATE_BEAR", "local/qwen2.5-32b")
    monkeypatch.setenv("ATLAS_LOCAL_LLM_URL", "http://192.168.1.50:8000")
    run_debate(session=None, audit=None, symbol="AVGO", evidence=EVIDENCE)
    (bull_c, _), (bear_c, _), (bull_reb_c, _), (bear_reb_c, _) = seen
    assert isinstance(bull_c, AnthropicClient)
    assert isinstance(bear_c, OpenAICompatClient)
    assert bull_reb_c is bull_c and bear_reb_c is bear_c   # rebuttal = own side
