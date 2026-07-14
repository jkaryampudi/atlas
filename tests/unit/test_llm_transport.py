"""Transport hygiene for the LLM clients (production perf defect, 2026-07-14).

Two regressions are pinned here so the fix cannot silently rot:

1. TIMEOUTS — a full max_tokens=2500 generation can run past 60s; the old
   httpx.Client(timeout=60) turned a slow-but-healthy body into a ReadTimeout
   that the runner retried 3x (~3x60s of dead waiting). The real clients must
   now carry a SHORT connect timeout (dead endpoints fail fast) and a GENEROUS
   read timeout (>= 180s) so a healthy large generation completes.

2. LEAK — build_client is called per symbol by the nightly desk; each call used
   to construct a fresh httpx.Client (a fresh connection pool) that was never
   closed, so a ~15-symbol cycle leaked ~45 pools. build_client must now REUSE
   one client per resolution for the process.

Pure unit: no Postgres, no network. Clients are constructed but never called
(construction is what allocates the pool / sets the timeout).
"""
from __future__ import annotations

import httpx
import pytest

import atlas.agents.runtime.runner as runner
from atlas.agents.runtime.llm import (
    CONNECT_TIMEOUT_S,
    AnthropicClient,
    OpenAICompatClient,
)
from atlas.agents.runtime.registry import (
    build_client,
    reset_client_cache,
)


# --- 1. Timeout config on both real clients ---------------------------------

def test_anthropic_client_timeout_split():
    """Short connect, generous read: the whole point of the fix."""
    t = AnthropicClient("key")._client.timeout
    assert t.connect == CONNECT_TIMEOUT_S == 10.0   # dead endpoint fails fast
    assert t.read >= 180.0                           # room for a 2500-token body
    # a genuinely-dead endpoint must not be able to hang for the read window
    assert t.connect < t.read
    assert t.pool == 10.0 and t.write == 30.0


def test_openai_compat_client_timeout_split():
    """Local models can be slow too — read timeout stays >= 180s."""
    t = OpenAICompatClient("http://192.168.1.50:8000", "qwen2.5-32b")._client.timeout
    assert t.connect == CONNECT_TIMEOUT_S == 10.0
    assert t.read >= 180.0
    assert t.connect < t.read


@pytest.mark.parametrize("factory", [
    lambda: AnthropicClient("key", read_timeout_s=42.0),
    lambda: OpenAICompatClient("http://x", "m", read_timeout_s=42.0),
])
def test_read_timeout_overridable_via_constructor(factory):
    """Tests can pin/override the read ceiling without wiring a transport."""
    t = factory()._client.timeout
    assert t.read == 42.0 and t.connect == CONNECT_TIMEOUT_S


def test_injected_transport_keeps_its_own_timeout():
    """The transport-injectable seam is untouched: an injected client wins."""
    injected = httpx.Client(timeout=httpx.Timeout(5.0))
    assert AnthropicClient("key", client=injected)._client is injected


# --- 2. build_client reuses one client per role (no pool leak) ---------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Deterministic Anthropic resolution for the reuse tests."""
    monkeypatch.delenv("ATLAS_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("ATLAS_MODEL_CIO", raising=False)
    monkeypatch.delenv("ATLAS_MODEL_SECTOR", raising=False)
    reset_client_cache()


def test_build_client_reuses_same_instance_per_role():
    """The leak fix: repeated builds for one role return the SAME client, so the
    nightly desk stops opening (and leaking) a connection pool per symbol."""
    first = build_client("cio", api_key="k")
    second = build_client("cio", api_key="k")
    assert first is second                       # reused, pool not re-created
    assert isinstance(first, AnthropicClient)


def test_cache_is_keyed_per_role():
    """Different roles still get their own clients (per-role routing intact)."""
    assert build_client("cio", api_key="k") is not build_client("sector", api_key="k")


def test_injected_http_client_bypasses_cache():
    """Injecting a transport (tests) builds fresh every time and never caches —
    per-test transports must not persist into other tests."""
    t1 = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    t2 = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    c1 = build_client("cio", api_key="k", http_client=t1)
    c2 = build_client("cio", api_key="k", http_client=t2)
    assert c1 is not c2
    # and the cached (no-inject) path is a distinct, reused instance
    cached = build_client("cio", api_key="k")
    assert cached is not c1 and cached is build_client("cio", api_key="k")


def test_reset_client_cache_forces_rebuild():
    """The reset seam actually drops the cache (test hygiene depends on it)."""
    first = build_client("cio", api_key="k")
    reset_client_cache()
    assert build_client("cio", api_key="k") is not first


# --- 3. A ReadTimeout is still classified TRANSIENT (behavior unchanged) ------

def test_read_timeout_still_transient():
    """The new generous read ceiling can still be hit; when it is, the runner
    must keep treating it as vendor plumbing (TRANSIENT), never a cage verdict —
    exactly the classification that predates this change."""
    assert runner._is_transient(httpx.ReadTimeout("read timed out")) is True
    assert runner._is_transient(httpx.ConnectTimeout("connect timed out")) is True
