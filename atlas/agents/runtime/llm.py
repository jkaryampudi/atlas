"""LLM client protocol + Anthropic implementation + deterministic stub for tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

# ---------------------------------------------------------------------------
# Transport timeouts (production perf defect observed 2026-07-14).
#
# WHY a long READ timeout: debate seats and the CIO committee memo generate at
# max_tokens=2500 (see roles/debate.py `_case` and roles/cio.py
# `committee_memo`). A full 2500-token completion can run well past 60s of wall
# time. The former httpx.Client(timeout=60) applied that single 60s ceiling to
# the READ phase, so a slow-but-HEALTHY generation raised httpx.ReadTimeout.
# The runner classifies that TRANSIENT (runner._is_transient) and retries up to
# TRANSIENT_MAX_ATTEMPTS (3) with backoff — turning one long generation into
# ~3x60s of dead waiting per symbol before a per-symbol skip, all while the
# nightly desk holds a single Postgres transaction open (atlas/ops/daily.py).
#
# The split below keeps genuinely-dead endpoints failing FAST (short connect
# timeout, still classified TRANSIENT and retried in seconds, not minutes)
# while giving a healthy large generation room to finish. This READ ceiling is
# a deliberate, reviewed constant sized for max_tokens=2500 — do NOT couple it
# programmatically to max_tokens; raising max_tokens is a separate reviewed
# decision (CLAUDE.md: do not change default max_tokens here).
ANTHROPIC_READ_TIMEOUT_S = 180.0   # headroom for a full max_tokens=2500 body
LOCAL_READ_TIMEOUT_S = 180.0       # LAN/3090 models can be slower still
CONNECT_TIMEOUT_S = 10.0           # dead endpoints must fail fast
WRITE_TIMEOUT_S = 30.0
POOL_TIMEOUT_S = 10.0


def _timeout(read_s: float) -> httpx.Timeout:
    """A short connect/pool timeout with a generous, explicit read timeout."""
    return httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=read_s,
                         write=WRITE_TIMEOUT_S, pool=POOL_TIMEOUT_S)


@dataclass(frozen=True)
class LlmResult:
    text: str
    tokens_in: int
    tokens_out: int
    model: str


class LlmClient(Protocol):
    def complete(self, prompt: str, *, max_tokens: int) -> LlmResult: ...


class AnthropicClient:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6",
                 client: httpx.Client | None = None, *,
                 read_timeout_s: float = ANTHROPIC_READ_TIMEOUT_S) -> None:
        self._key = api_key
        self._model = model
        # `client` keeps the transport-injectable seam for tests; `read_timeout_s`
        # lets a test pin/override the read ceiling without wiring a transport.
        self._client = client or httpx.Client(timeout=_timeout(read_timeout_s))

    def complete(self, prompt: str, *, max_tokens: int) -> LlmResult:
        r = self._client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self._key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self._model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return LlmResult(text=text, tokens_in=u.get("input_tokens", 0),
                         tokens_out=u.get("output_tokens", 0), model=d.get("model", self._model))


class OpenAICompatClient:
    """OpenAI-compatible /v1/chat/completions client (local models on the LAN,
    e.g. a 3090 box; ADR-0005 pattern 4). Transport-injectable for tests."""

    def __init__(self, base_url: str, model: str, api_key: str = "local",
                 client: httpx.Client | None = None, *,
                 read_timeout_s: float = LOCAL_READ_TIMEOUT_S) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._key = api_key
        self._client = client or httpx.Client(timeout=_timeout(read_timeout_s))

    def complete(self, prompt: str, *, max_tokens: int) -> LlmResult:
        r = self._client.post(
            f"{self._base}/v1/chat/completions",
            headers={"authorization": f"Bearer {self._key}",
                     "content-type": "application/json"},
            json={"model": self._model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        d = r.json()
        u = d.get("usage", {})
        return LlmResult(text=d["choices"][0]["message"]["content"],
                         tokens_in=u.get("prompt_tokens", 0),
                         tokens_out=u.get("completion_tokens", 0),
                         model=d.get("model", self._model))


class StubClient:
    """Deterministic client for tests and red-teaming: returns scripted outputs."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, max_tokens: int) -> LlmResult:
        self.prompts.append(prompt)
        text = self._responses.pop(0) if self._responses else "{}"
        return LlmResult(text=text, tokens_in=len(prompt) // 4,
                         tokens_out=len(text) // 4, model="stub")
