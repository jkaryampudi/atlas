"""Per-role model registry (ADR-0005 pattern 4; Constitution 7.2 in code).

Resolution: ATLAS_MODEL_<ROLE> -> ATLAS_MODEL_DEFAULT -> built-in default.
A model string prefixed "local/" routes to the OpenAI-compatible client at
ATLAS_LOCAL_LLM_URL. The resolved model string is recorded per run in
research.agent_runs.model (done by the runner); model upgrades run in
shadow_mode first (logged, non-actionable) before sign-off.
"""
from __future__ import annotations

import os

import httpx

from atlas.agents.runtime.llm import AnthropicClient, LlmClient, OpenAICompatClient

DEFAULT_MODEL = "claude-sonnet-4-6"
LOCAL_PREFIX = "local/"


def resolve_model(role: str) -> str:
    key = f"ATLAS_MODEL_{role.upper().replace('-', '_')}"
    return (os.environ.get(key)
            or os.environ.get("ATLAS_MODEL_DEFAULT")
            or DEFAULT_MODEL)


def build_client(role: str, *, api_key: str | None = None,
                 http_client: httpx.Client | None = None) -> LlmClient:
    """Client for a role's resolved model. `http_client` is injectable for tests."""
    model = resolve_model(role)
    if model.startswith(LOCAL_PREFIX):
        base = os.environ.get("ATLAS_LOCAL_LLM_URL", "")
        if not base:
            raise ValueError(f"model {model!r} for role {role!r} requires "
                             "ATLAS_LOCAL_LLM_URL")
        return OpenAICompatClient(base_url=base, model=model[len(LOCAL_PREFIX):],
                                  client=http_client)
    key = api_key if api_key is not None else os.environ.get("ATLAS_ANTHROPIC_API_KEY", "")
    return AnthropicClient(key, model=model, client=http_client)
