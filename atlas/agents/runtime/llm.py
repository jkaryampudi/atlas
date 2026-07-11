"""LLM client protocol + Anthropic implementation + deterministic stub for tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


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
                 client: httpx.Client | None = None) -> None:
        self._key = api_key
        self._model = model
        self._client = client or httpx.Client(timeout=60)

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
                 client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._key = api_key
        self._client = client or httpx.Client(timeout=120)

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
