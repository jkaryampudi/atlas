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
from atlas.agents.runtime.llm import LlmClient
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.config import get_settings

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


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
              max_tokens: int = 1200) -> tuple[BaseModel, str]:
    template, t_hash = load_template(template_rel_path)
    prompt = template + "\n\n" + context
    last_err = ""
    for attempt in (1, 2):
        result = client.complete(prompt, max_tokens=max_tokens)
        cost = (result.tokens_in * 3 + result.tokens_out * 15) / 1_000_000
        try:
            spend_and_check(session, cost_usd=cost,
                            daily_cap_usd=get_settings().daily_llm_budget_usd)
        except BudgetExhausted:
            session.execute(text(
                "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
                " model, status, cost_usd) VALUES (:r, :h, :m, 'budget_kill', :c)"),
                {"r": agent_role, "h": t_hash, "m": result.model, "c": cost})
            audit.append(event_type="cost.budget.breached", entity_type="agent_run",
                         entity_id=agent_role, actor_type="scheduler",
                         actor_id="budget_guard", payload={"cost": cost})
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
        run_id = session.execute(text(
            "INSERT INTO research.agent_runs "
            "(agent_role, prompt_template_hash, model, input_refs, output_hash, status, "
            " tokens_in, tokens_out, cost_usd) "
            "VALUES (:r, :h, :m, CAST(:i AS jsonb), :oh, :s, :ti, :to, :c) RETURNING id"),
            {"r": agent_role, "h": t_hash, "m": result.model,
             "i": json.dumps(input_refs),
             "oh": hashlib.sha256(raw.encode()).hexdigest(), "s": status,
             "ti": result.tokens_in, "to": result.tokens_out,
             "c": cost}
        ).scalar_one()
        audit.append(event_type="agent.run.completed", entity_type="agent_run",
                     entity_id=str(run_id), actor_type="agent", actor_id=agent_role,
                     payload={"status": status, "template_hash": t_hash[:12],
                              "attempt": attempt})
        if validated is not None:
            return validated, str(run_id)
    raise AgentRunFailed(f"{agent_role}: two consecutive schema failures — {last_err[:300]}")
