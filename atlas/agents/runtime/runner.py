"""Agent run orchestration: prompt hash-pinning, call, schema validation, persistence,
audit event. One retry on schema failure, then the run fails closed (Constitution 5.2)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.runtime.budget import BudgetExhausted, spend_and_check
from atlas.agents.runtime.grounding import grounding_violations
from atlas.agents.runtime.llm import LlmClient, OpenAICompatClient
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.config import get_settings

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"

# ---------------------------------------------------------------------------
# Per-model pricing (desk-review 2026-07 item 3). The $10/day budget breaker
# (Constitution 5.4) is a constitutional control: it must never undercount a
# model. Rates are USD per 1M tokens (input, output).
#
# Source: Anthropic published pricing, platform.claude.com/docs/en/pricing,
# retrieved 2026-07-13 (Opus 4.6/4.7/4.8 $5/$25; Sonnet 4.6/5 $3/$15;
# Haiku 4.5 $1/$5). This is a REVIEWED constant — update deliberately with a
# dated source, never scrape.
#
# Matching is by prefix so dated snapshot ids (e.g. claude-haiku-4-5-20251001)
# price like their alias. Deliberately NO bare "claude-opus-" catch-all: an
# Opus version we have not priced FAILS CLOSED below rather than assuming
# today's rate (legacy Opus 4.1/4.0 were $15/$75 — 3x today's Opus).
#
# The local route (registry model string "local/<name>", served by
# OpenAICompatClient on the LAN) is $0.00 API spend — electricity, not
# dollars; tokens are still recorded on the run row.
#
# Unknown models FAIL CLOSED: billed at the highest rate on Anthropic's
# published price list (legacy Opus 4.1, $15/$75) and flagged in the audit
# payload — over-counting spend is safe, undercounting breaches the breaker.
LOCAL_MODEL_PREFIX = "local/"
MODEL_RATES_PER_MTOK: tuple[tuple[str, tuple[float, float]], ...] = (
    (LOCAL_MODEL_PREFIX, (0.0, 0.0)),
    ("claude-opus-4-8", (5.0, 25.0)),
    ("claude-opus-4-7", (5.0, 25.0)),
    ("claude-opus-4-6", (5.0, 25.0)),
    ("claude-sonnet-5", (3.0, 15.0)),
    ("claude-sonnet-4-6", (3.0, 15.0)),
    ("claude-haiku-4-5", (1.0, 5.0)),
)
FAIL_CLOSED_RATE_PER_MTOK: tuple[float, float] = (15.0, 75.0)


def price_model(model: str) -> tuple[float, float, bool]:
    """(usd_per_mtok_in, usd_per_mtok_out, fail_closed) for a model string.
    Unknown models bill at FAIL_CLOSED_RATE_PER_MTOK with the flag set."""
    for prefix, (rate_in, rate_out) in MODEL_RATES_PER_MTOK:
        if model.startswith(prefix):
            return rate_in, rate_out, False
    rate_in, rate_out = FAIL_CLOSED_RATE_PER_MTOK
    return rate_in, rate_out, True


def load_template(rel_path: str) -> tuple[str, str]:
    """Returns (template_text, sha256 hash). Constitution is always prepended."""
    constitution = (PROMPTS / "constitution.md").read_text()
    template = (PROMPTS / rel_path).read_text()
    full = constitution + "\n\n" + template
    return full, hashlib.sha256(full.encode()).hexdigest()


class AgentRunFailed(Exception):
    pass


def run_agent(*, session: Session, audit: PostgresAuditLog, client: LlmClient,
              agent_role: str, template_rel_path: str, context: str,
              output_model: type[BaseModel], input_refs: list[dict[str, str]],
              extra_fields: dict[str, object] | None = None,
              evidence_bodies: dict[str, str] | None = None,
              shadow_mode: bool = False,
              max_tokens: int = 1200) -> tuple[BaseModel, str]:
    """shadow_mode (Constitution 7.2): the run executes and is fully logged but
    is marked non-actionable — for model-upgrade shadow periods. Callers must
    not persist memos or act on shadow outputs."""
    template, t_hash = load_template(template_rel_path)
    prompt = template + "\n\n" + context
    last_err = ""
    for attempt in (1, 2):
        result = client.complete(prompt, max_tokens=max_tokens)
        model_str = result.model
        if (isinstance(client, OpenAICompatClient)
                and not model_str.startswith(LOCAL_MODEL_PREFIX)):
            # The registry resolves the local route as "local/<name>" but the
            # client strips the prefix before calling the server; restore it
            # so the run row records the ROUTE (registry.py docstring: the
            # resolved model string is recorded per run) and pricing can
            # never bill LAN inference as vendor spend.
            model_str = LOCAL_MODEL_PREFIX + model_str
        rate_in, rate_out, fail_closed = price_model(model_str)
        cost = (result.tokens_in * rate_in + result.tokens_out * rate_out) / 1_000_000
        # The rate pair used is persisted per run via the audit payloads below
        # (agent_runs has no rate columns; no migration — desk-review item 3).
        pricing = {"model": model_str, "rate_in_per_mtok": rate_in,
                   "rate_out_per_mtok": rate_out,
                   "pricing_fail_closed": fail_closed}
        try:
            spend_and_check(session, cost_usd=cost,
                            daily_cap_usd=get_settings().daily_llm_budget_usd)
        except BudgetExhausted:
            session.execute(text(
                "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
                " model, status, cost_usd) VALUES (:r, :h, :m, 'budget_kill', :c)"),
                {"r": agent_role, "h": t_hash, "m": model_str, "c": cost})
            audit.append(event_type="cost.budget.breached", entity_type="agent_run",
                         entity_id=agent_role, actor_type="scheduler",
                         actor_id="budget_guard", payload={"cost": cost,
                                                           "pricing": pricing})
            raise
        raw = result.text.replace("```json", "").replace("```", "").strip()
        try:
            payload = json.loads(raw)
            if extra_fields:
                payload.update(extra_fields)   # runtime-injected flags, not model-controlled
            validated = output_model.model_validate(payload)
            status = "ok"
        except (json.JSONDecodeError, ValidationError) as e:
            last_err = str(e)
            status = "schema_fail"
            validated = None
        if validated is not None and evidence_bodies is not None:
            # ADR-0005 pattern 2: fabricated numbers take the schema_fail path
            # (retry once, then fail closed) with a dedicated audit event
            violations = grounding_violations(validated, evidence_bodies)
            if violations:
                last_err = "; ".join(violations)
                status = "schema_fail"
                validated = None
                audit.append(event_type="agent.grounding.failed",
                             entity_type="agent_run", entity_id=agent_role,
                             actor_type="agent", actor_id=agent_role,
                             payload={"attempt": attempt,
                                      "violations": violations[:10]})
        run_id = session.execute(text(
            "INSERT INTO research.agent_runs "
            "(agent_role, prompt_template_hash, model, input_refs, output_hash, status, "
            " tokens_in, tokens_out, cost_usd, shadow) "
            "VALUES (:r, :h, :m, CAST(:i AS jsonb), :oh, :s, :ti, :to, :c, :sh) "
            "RETURNING id"),
            {"r": agent_role, "h": t_hash, "m": model_str,
             "i": json.dumps(input_refs),
             "oh": hashlib.sha256(raw.encode()).hexdigest(), "s": status,
             "ti": result.tokens_in, "to": result.tokens_out,
             "c": cost, "sh": shadow_mode}
        ).scalar_one()
        audit.append(event_type="agent.run.completed", entity_type="agent_run",
                     entity_id=str(run_id), actor_type="agent", actor_id=agent_role,
                     payload={"status": status, "template_hash": t_hash[:12],
                              "attempt": attempt, "shadow": shadow_mode,
                              "pricing": pricing})
        if validated is not None:
            return validated, str(run_id)
    raise AgentRunFailed(f"{agent_role}: two consecutive schema failures — {last_err[:300]}")
