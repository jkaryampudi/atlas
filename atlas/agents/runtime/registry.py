"""Per-role model registry (ADR-0005 pattern 4; Constitution 7.2 in code).

Resolution: ATLAS_MODEL_<ROLE> -> ATLAS_MODEL_DEFAULT -> built-in default.
A model string prefixed "local/" routes to the OpenAI-compatible client at
ATLAS_LOCAL_LLM_URL. The resolved model string is recorded per run in
research.agent_runs.model (done by the runner); model upgrades run in
shadow_mode first (logged, non-actionable) before sign-off.

Client REUSE (production perf defect observed 2026-07-14): build_client is
called PER SYMBOL by the nightly desk — run_debate builds a debate_bull +
debate_bear client and run_desk builds a cio client on every iteration
(atlas/agents/desk.py). Each AnthropicClient/OpenAICompatClient owns an
httpx.Client with its own connection pool, and none were ever closed; a
~15-symbol cycle therefore leaked ~45 pools (observed: ~45 lingering
CLOSE_WAIT/ESTABLISHED sockets to api.anthropic.com). We now cache one client
per (role, resolved model, api_key, local URL) for the process, so httpx
keep-alive is reused across symbols and the pool count stays bounded. Only the
env/registry-resolved path is cached: when a caller injects its own
http_client (tests), we build fresh and never cache, keeping the transport
seam intact and per-test transports from persisting across tests.
"""
from __future__ import annotations

import os
import threading

import httpx

from atlas.agents.runtime.llm import AnthropicClient, LlmClient, OpenAICompatClient

DEFAULT_MODEL = "claude-sonnet-4-6"
LOCAL_PREFIX = "local/"

_CLIENT_CACHE: dict[tuple[object, ...], LlmClient] = {}
_CACHE_LOCK = threading.Lock()


def reset_client_cache() -> None:
    """Drop every cached client. A manual reset seam and test-hygiene hook:
    the cache is process-global, so tests that exercise the real registry clear
    it to stay order-independent."""
    with _CACHE_LOCK:
        _CLIENT_CACHE.clear()


def resolve_model(role: str) -> str:
    key = f"ATLAS_MODEL_{role.upper().replace('-', '_')}"
    return (os.environ.get(key)
            or os.environ.get("ATLAS_MODEL_DEFAULT")
            or DEFAULT_MODEL)


def _construct_client(role: str, model: str, api_key: str | None,
                      http_client: httpx.Client | None) -> LlmClient:
    if model.startswith(LOCAL_PREFIX):
        base = os.environ.get("ATLAS_LOCAL_LLM_URL", "")
        if not base:
            raise ValueError(f"model {model!r} for role {role!r} requires "
                             "ATLAS_LOCAL_LLM_URL")
        return OpenAICompatClient(base_url=base, model=model[len(LOCAL_PREFIX):],
                                  client=http_client)
    key = api_key if api_key is not None else os.environ.get("ATLAS_ANTHROPIC_API_KEY", "")
    return AnthropicClient(key, model=model, client=http_client)


def build_client(role: str, *, api_key: str | None = None,
                 http_client: httpx.Client | None = None) -> LlmClient:
    """Client for a role's resolved model. Reused per process (see module note)
    to avoid leaking one connection pool per call. `http_client` is injectable
    for tests; when it is supplied we build fresh and skip the cache."""
    model = resolve_model(role)
    if http_client is not None:
        return _construct_client(role, model, api_key, http_client)
    # Key on everything that determines the constructed client so a changed env
    # (or an explicit api_key in a test) never returns a stale instance. The
    # local URL is part of the key; the empty-URL error still surfaces because
    # _construct_client raises before anything is cached.
    local_url = (os.environ.get("ATLAS_LOCAL_LLM_URL", "")
                 if model.startswith(LOCAL_PREFIX) else "")
    cache_key = (role, model, api_key, local_url)
    with _CACHE_LOCK:
        cached = _CLIENT_CACHE.get(cache_key)
        if cached is not None:
            return cached
        client = _construct_client(role, model, api_key, None)
        _CLIENT_CACHE[cache_key] = client
        return client
