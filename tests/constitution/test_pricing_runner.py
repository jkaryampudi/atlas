"""Runner pricing through the cage (desk-review 2026-07 item 3): the run row
records the routed model string, the cost uses that model's rate pair, the
rate pair is persisted per run in the audit payload, unknown models fail
closed (flagged), and the local route is $0.00 with tokens still recorded."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import text

from atlas.agents.runtime.llm import OpenAICompatClient, StubClient
from atlas.agents.runtime.runner import run_agent
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import FrozenClock
from tests.conftest import requires_pg

pytestmark = requires_pg


class _Probe(BaseModel):
    note: str


def _audit(s):
    return PostgresAuditLog(s, FrozenClock(datetime(2026, 7, 13, 6, 0, tzinfo=UTC)))


def _run(s, client):
    return run_agent(session=s, audit=_audit(s), client=client,
                     agent_role="pricing_probe",
                     template_rel_path="cio/committee_memo.md",
                     context="pricing probe", output_model=_Probe,
                     input_refs=[])


def _row(s):
    return s.execute(text(
        "SELECT model, tokens_in, tokens_out, cost_usd FROM research.agent_runs "
        "WHERE agent_role = 'pricing_probe'")).one()


def _pricing_payload(s):
    payload = s.execute(text(
        "SELECT payload FROM audit.decision_events "
        "WHERE event_type = 'agent.run.completed'")).scalar()
    return payload["pricing"]


def test_unknown_model_fails_closed_flagged_and_rate_pair_persisted(clean_audit):
    s = clean_audit
    out, _ = _run(s, StubClient([json.dumps({"note": "ok"})]))
    assert out.note == "ok"
    row = _row(s)
    assert row.model == "stub"                     # recorded verbatim, never guessed
    expected = (row.tokens_in * 15.0 + row.tokens_out * 75.0) / 1_000_000
    assert float(row.cost_usd) == pytest.approx(expected, abs=5e-5)  # numeric(10,4)
    assert _pricing_payload(s) == {"model": "stub",
                                   "rate_in_per_mtok": 15.0,
                                   "rate_out_per_mtok": 75.0,
                                   "pricing_fail_closed": True}


def test_local_route_is_zero_dollars_with_tokens_recorded(clean_audit):
    s = clean_audit

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "qwen2.5-32b",
            "choices": [{"message": {"role": "assistant",
                                     "content": json.dumps({"note": "ok"})}}],
            "usage": {"prompt_tokens": 120000, "completion_tokens": 30000}})

    client = OpenAICompatClient(
        "http://local:8000", "qwen2.5-32b",
        client=httpx.Client(transport=httpx.MockTransport(handler)))
    _run(s, client)
    row = _row(s)
    assert row.model == "local/qwen2.5-32b"        # the ROUTE is the record
    assert (row.tokens_in, row.tokens_out) == (120000, 30000)
    assert float(row.cost_usd) == 0.0              # electricity, not API spend
    assert _pricing_payload(s) == {"model": "local/qwen2.5-32b",
                                   "rate_in_per_mtok": 0.0,
                                   "rate_out_per_mtok": 0.0,
                                   "pricing_fail_closed": False}
