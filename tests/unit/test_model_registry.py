"""Per-role model registry + OpenAI-compatible client (ADR-0005 pattern 4)."""
import json

import httpx
import pytest

from atlas.agents.runtime.llm import AnthropicClient, OpenAICompatClient
from atlas.agents.runtime.registry import DEFAULT_MODEL, build_client, resolve_model


def test_resolution_precedence(monkeypatch):
    monkeypatch.delenv("ATLAS_MODEL_CIO", raising=False)
    monkeypatch.delenv("ATLAS_MODEL_DEFAULT", raising=False)
    assert resolve_model("cio") == DEFAULT_MODEL
    monkeypatch.setenv("ATLAS_MODEL_DEFAULT", "claude-haiku-4-5")
    assert resolve_model("cio") == "claude-haiku-4-5"
    monkeypatch.setenv("ATLAS_MODEL_CIO", "claude-opus-4-8")
    assert resolve_model("cio") == "claude-opus-4-8"
    assert resolve_model("scanner") == "claude-haiku-4-5"  # falls back to default


def test_build_client_anthropic_by_default(monkeypatch):
    monkeypatch.delenv("ATLAS_MODEL_SECTOR", raising=False)
    monkeypatch.delenv("ATLAS_MODEL_DEFAULT", raising=False)
    client = build_client("sector", api_key="k")
    assert isinstance(client, AnthropicClient)


def test_build_client_local_routes_to_openai_compat(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_SCANNER", "local/qwen2.5-32b")
    monkeypatch.setenv("ATLAS_LOCAL_LLM_URL", "http://192.168.1.50:8000")
    client = build_client("scanner")
    assert isinstance(client, OpenAICompatClient)


def test_build_client_local_without_url_fails(monkeypatch):
    monkeypatch.setenv("ATLAS_MODEL_SCANNER", "local/qwen2.5-32b")
    monkeypatch.delenv("ATLAS_LOCAL_LLM_URL", raising=False)
    with pytest.raises(ValueError, match="ATLAS_LOCAL_LLM_URL"):
        build_client("scanner")


def test_openai_compat_client_parses_chat_completions():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "qwen2.5-32b",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    c = OpenAICompatClient("http://local:8000/", "qwen2.5-32b",
                           client=httpx.Client(transport=httpx.MockTransport(handler)))
    r = c.complete("hi", max_tokens=64)
    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"]["model"] == "qwen2.5-32b"
    assert seen["body"]["max_tokens"] == 64
    assert r.text == "hello" and r.tokens_in == 12 and r.tokens_out == 3
    assert r.model == "qwen2.5-32b"
