"""Agent run orchestration: prompt hash-pinning, call, schema validation, persistence,
audit event. One retry on schema failure — with the violation text appended via a
reviewed template — then the run fails closed (Constitution 5.2). Transient
transport failures (429/5xx/timeout) back off and retry in place, then surface as
TransientLlmFailure; budget checks bind the global breaker first, then the
calling surface's sub-cap (desk-review 2026-07 item 6)."""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.runtime.budget import BudgetExhausted, spend_and_check
from atlas.agents.runtime.grounding import grounding_violations
from atlas.agents.runtime.llm import LlmClient, LlmResult, OpenAICompatClient
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


# ---------------------------------------------------------------------------
# Failure semantics (desk-review 2026-07 item 6): TRANSIENT vs CAGE.
#
# TRANSIENT failures are vendor plumbing — HTTP 429 (rate limit), any 5xx, or
# a client-side timeout. They say nothing about the model's honesty, so each
# completion call retries IN PLACE with bounded exponential backoff, then
# raises TransientLlmFailure for the desk to record as a per-symbol
# 'transient' skip (never a cage hold, never a shortlist abort). Nothing else
# is transient: 4xx besides 429 is a configuration bug and propagates raw.
#
# CAGE verdicts (schema/grounding kills) are the system working and keep the
# one-retry-then-fail-closed loop in run_agent, now with the recorded
# violation text appended to the retry via the reviewed template
# prompts/retry/violation.md (see load_retry_template).
#
# Backoff constants (deliberate, documented):
TRANSIENT_MAX_ATTEMPTS = 3         # 1 call + up to 2 retries per completion
TRANSIENT_BACKOFF_BASE_S = 1.0     # sleeps ~1s then ~2s (exponential, base 2)
TRANSIENT_BACKOFF_JITTER_S = 0.25  # + uniform [0, 0.25) s de-synchronization
_sleep = time.sleep                # module hooks: tests replace to observe
_jitter = random.random


class TransientLlmFailure(Exception):
    """The LLM transport failed transiently after bounded retries — an
    infrastructure outcome, never a cage verdict."""


def _is_transient(exc: BaseException) -> bool:
    """Exactly HTTP 429 / 5xx / client-side timeouts (item 6) — nothing else."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _complete_with_backoff(client: LlmClient, prompt: str, *,
                           max_tokens: int) -> LlmResult:
    """One logical completion with bounded, jittered exponential backoff on
    transient transport failures. A never-completed call carries no vendor
    usage data, so there is no cost to persist — the budget tally is untouched
    by definition, not by omission."""
    for attempt in range(1, TRANSIENT_MAX_ATTEMPTS + 1):
        try:
            return client.complete(prompt, max_tokens=max_tokens)
        except Exception as e:
            if not _is_transient(e):
                raise
            if attempt == TRANSIENT_MAX_ATTEMPTS:
                raise TransientLlmFailure(
                    f"transient LLM failure after {TRANSIENT_MAX_ATTEMPTS} "
                    f"attempts: {type(e).__name__}: {str(e)[:200]}") from e
            _sleep(TRANSIENT_BACKOFF_BASE_S * 2 ** (attempt - 1)
                   + _jitter() * TRANSIENT_BACKOFF_JITTER_S)
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Per-surface budget sub-caps (desk-review 2026-07 item 6).
#
# PRECEDENCE: the global daily breaker (ATLAS_DAILY_LLM_BUDGET_USD, $10 —
# Constitution 5.4) ALWAYS wins. It is checked first and binds every run,
# surface or no surface; a sub-cap set above it changes nothing.
#
# A sub-cap is a WATERMARK on the same shared daily tally, not an attributed
# per-surface meter (research.agent_runs has no surface column; attribution
# would be a schema change deliberately out of this change's scope): a surface
# may not push the day's total spend past its watermark. That one-directional
# rule is the starvation guarantee the desk review asked for — ANALYZE
# (default $3.00) can never take the day past $3.00, so the 23:30 UTC nightly
# desk always finds at least global-minus-analyze headroom; the NIGHTLY
# watermark ($6.00) bounds the desk itself, leaving the breaker's last slice
# as slack. The reverse starvation (a heavy nightly blocking later analyze
# clicks) is deliberate: the nightly desk is the priority surface.
#
# Surfaces are bound where each one enters the runner: run_desk defaults to
# 'nightly' (the T7 cycle and manual desk runs), atlas/ops/analyze.py wraps
# its desk call in budget_surface('analyze'), and the shadow model-upgrade
# comparison (atlas/agents/shadow_compare.py, Constitution 7.2) binds
# 'shadow' (ATLAS_BUDGET_SHADOW, default $3.00) — a comparison spree must
# never starve the nightly desk, exactly the analyze rationale. Entry points
# that bind no surface (e.g. the manual live_run tool) answer to the global
# breaker alone.
SURFACE_BUDGET_DEFAULTS_USD: dict[str, float] = {"nightly": 6.00, "analyze": 3.00,
                                                 "shadow": 3.00}
_budget_surface: ContextVar[str | None] = ContextVar("atlas_budget_surface",
                                                     default=None)


def current_budget_surface() -> str | None:
    return _budget_surface.get()


@contextmanager
def budget_surface(surface: str) -> Iterator[None]:
    """Bind every run_agent call in this context to a surface sub-cap."""
    token = _budget_surface.set(surface)
    try:
        yield
    finally:
        _budget_surface.reset(token)


def surface_cap_usd(surface: str) -> float:
    """ATLAS_BUDGET_NIGHTLY / ATLAS_BUDGET_ANALYZE, else the documented
    defaults. An unknown surface with no env var fails loudly (KeyError):
    a surface nobody budgeted must not silently inherit the global cap."""
    raw = os.environ.get(f"ATLAS_BUDGET_{surface.upper()}")
    if raw is not None:
        return float(raw)
    return SURFACE_BUDGET_DEFAULTS_USD[surface]


def load_template(rel_path: str) -> tuple[str, str]:
    """Returns (template_text, sha256 hash). Constitution is always prepended."""
    constitution = (PROMPTS / "constitution.md").read_text()
    template = (PROMPTS / rel_path).read_text()
    full = constitution + "\n\n" + template
    return full, hashlib.sha256(full.encode()).hexdigest()


RETRY_TEMPLATE_REL_PATH = "retry/violation.md"


def load_retry_template() -> tuple[str, str]:
    """(template_text, sha256) for the cage-retry addendum. Unlike
    load_template, no constitution prefix: this text is APPENDED to a prompt
    that already carries the constitution. Prompts are code (CLAUDE.md
    invariant 5): the addendum wording is a reviewed file, and the hash used
    is recorded in the attempt-2 audit payload (retry_template_hash)."""
    template = (PROMPTS / RETRY_TEMPLATE_REL_PATH).read_text()
    return template, hashlib.sha256(template.encode()).hexdigest()


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
        attempt_prompt, retry_hash = prompt, None
        if attempt == 2:
            # Cage retry (item 6): the recorded violation text rides the
            # reviewed addendum template. It quotes model output, so it is
            # DATA — fence impersonation neutralized exactly like untrusted
            # evidence (untrusted.py), then bounded.
            retry_template, retry_hash = load_retry_template()
            safe_err = last_err.replace("<<<", "«").replace(">>>", "»")[:500]
            attempt_prompt = (prompt + "\n\n"
                              + retry_template.replace("{violations}", safe_err))
        result = _complete_with_backoff(client, attempt_prompt,
                                        max_tokens=max_tokens)
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
        cap_scope = "global"
        try:
            # PRECEDENCE (item 6): the global breaker is checked first and
            # always wins; the surface watermark below can only be stricter.
            total = spend_and_check(session, cost_usd=cost,
                                    daily_cap_usd=get_settings().daily_llm_budget_usd)
            surface = current_budget_surface()
            if surface is not None:
                cap = surface_cap_usd(surface)
                if total > cap:
                    cap_scope = f"surface:{surface}"
                    raise BudgetExhausted(
                        f"surface budget breached ({surface}): day total "
                        f"{total:.2f} > {cap:.2f} USD sub-cap "
                        f"(ATLAS_BUDGET_{surface.upper()}; global cap intact)")
        except BudgetExhausted:
            # the kill row carries the shadow flag too (Constitution 7.2): a
            # shadow comparison must never write an unmarked run row, and the
            # comparison's cost attribution sums shadow-marked rows only
            session.execute(text(
                "INSERT INTO research.agent_runs (agent_role, prompt_template_hash, "
                " model, status, cost_usd, shadow) "
                "VALUES (:r, :h, :m, 'budget_kill', :c, :sh)"),
                {"r": agent_role, "h": t_hash, "m": model_str, "c": cost,
                 "sh": shadow_mode})
            audit.append(event_type="cost.budget.breached", entity_type="agent_run",
                         entity_id=agent_role, actor_type="scheduler",
                         actor_id="budget_guard", payload={"cost": cost,
                                                           "scope": cap_scope,
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
        run_payload: dict[str, object] = {
            "status": status, "template_hash": t_hash[:12],
            "attempt": attempt, "shadow": shadow_mode, "pricing": pricing}
        if retry_hash is not None:
            # the retry addendum is part of what the model read on attempt 2 —
            # pin its template hash on the chain (prompts are code)
            run_payload["retry_template_hash"] = retry_hash[:12]
        audit.append(event_type="agent.run.completed", entity_type="agent_run",
                     entity_id=str(run_id), actor_type="agent", actor_id=agent_role,
                     payload=run_payload)
        if validated is not None:
            return validated, str(run_id)
    raise AgentRunFailed(f"{agent_role}: two consecutive schema failures — {last_err[:300]}")
