"""Transient-vs-cage classification and bounded backoff (desk-review 2026-07
item 6), plus the per-surface budget sub-cap resolution — all pure unit, no
Postgres, no network: transport failures are simulated with real httpx
exception objects and the backoff sleeps are captured, never slept."""
from __future__ import annotations

import hashlib

import httpx
import pytest

import atlas.agents.runtime.runner as runner
from atlas.agents.runtime.llm import LlmResult
from atlas.agents.runtime.runner import (
    RETRY_TEMPLATE_REL_PATH,
    TRANSIENT_BACKOFF_BASE_S,
    TRANSIENT_BACKOFF_JITTER_S,
    TRANSIENT_MAX_ATTEMPTS,
    TransientLlmFailure,
    _complete_with_backoff,
    _is_transient,
    budget_surface,
    current_budget_surface,
    load_retry_template,
    surface_cap_usd,
)


def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.test/v1/messages")
    return httpx.HTTPStatusError(f"HTTP {code}", request=req,
                                 response=httpx.Response(code, request=req))


# --- classification: exactly 429 / 5xx / timeouts, nothing else -------------

@pytest.mark.parametrize("exc,expected", [
    (_http_error(429), True),                       # rate limit
    (_http_error(500), True),                       # server error
    (_http_error(503), True),                       # overloaded
    (_http_error(529), True),                       # anthropic overloaded
    (httpx.ReadTimeout("read timed out"), True),    # client-side timeout
    (httpx.ConnectTimeout("connect timed out"), True),
    (_http_error(400), False),                      # our bug, not transient
    (_http_error(401), False),                      # key problem — page, don't retry
    (_http_error(404), False),
    (ValueError("not http at all"), False),
])
def test_transient_classification(exc, expected):
    assert _is_transient(exc) is expected


# --- bounded, jittered exponential backoff ----------------------------------

class _FlakyClient:
    """Raises the scripted exceptions in order, then returns a result."""

    def __init__(self, failures: list[BaseException]) -> None:
        self._failures = list(failures)
        self.calls = 0

    def complete(self, prompt: str, *, max_tokens: int) -> LlmResult:
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return LlmResult(text="{}", tokens_in=1, tokens_out=1, model="stub")


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(runner, "_sleep", recorded.append)
    monkeypatch.setattr(runner, "_jitter", lambda: 0.5)  # deterministic jitter
    return recorded


def test_backoff_recovers_after_transient_failures(sleeps):
    client = _FlakyClient([_http_error(429), _http_error(503)])
    result = _complete_with_backoff(client, "p", max_tokens=10)
    assert result.model == "stub"
    assert client.calls == TRANSIENT_MAX_ATTEMPTS  # 2 failures + 1 success
    # exponential base-2 schedule with the documented jitter term
    assert sleeps == [
        TRANSIENT_BACKOFF_BASE_S * 1 + 0.5 * TRANSIENT_BACKOFF_JITTER_S,
        TRANSIENT_BACKOFF_BASE_S * 2 + 0.5 * TRANSIENT_BACKOFF_JITTER_S,
    ]


def test_backoff_is_bounded_then_raises_transient(sleeps):
    client = _FlakyClient([_http_error(503)] * 10)
    with pytest.raises(TransientLlmFailure, match="after 3 attempts.*HTTP 503"):
        _complete_with_backoff(client, "p", max_tokens=10)
    assert client.calls == TRANSIENT_MAX_ATTEMPTS   # bounded: never a 4th call
    assert len(sleeps) == TRANSIENT_MAX_ATTEMPTS - 1


def test_jitter_stays_within_documented_bound(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(runner, "_sleep", recorded.append)
    # real RNG: run several rounds and assert every sleep obeys the bound
    for _ in range(20):
        recorded.clear()
        with pytest.raises(TransientLlmFailure):
            _complete_with_backoff(_FlakyClient([_http_error(429)] * 5), "p",
                                   max_tokens=10)
        for i, s in enumerate(recorded):
            base = TRANSIENT_BACKOFF_BASE_S * 2 ** i
            assert base <= s < base + TRANSIENT_BACKOFF_JITTER_S


def test_non_transient_error_propagates_immediately(sleeps):
    client = _FlakyClient([_http_error(401)])
    with pytest.raises(httpx.HTTPStatusError, match="HTTP 401"):
        _complete_with_backoff(client, "p", max_tokens=10)
    assert client.calls == 1 and sleeps == []       # no retry, no sleep


def test_timeout_then_success(sleeps):
    client = _FlakyClient([httpx.ReadTimeout("slow upstream")])
    assert _complete_with_backoff(client, "p", max_tokens=10).text == "{}"
    assert client.calls == 2


# --- retry addendum template is a reviewed, hashed artifact ------------------

def test_retry_template_is_hash_pinned():
    """Prompts are code (CLAUDE.md invariant 5): changing the cage-retry
    addendum must break this pin and go through review."""
    text, sha = load_retry_template()
    assert "{violations}" in text                   # substitution point intact
    assert "DATA" in text                           # violations framed as data
    assert sha == ("648fe4ee2182404481812802fb55cf77"
                   "02a17bbdeff4e631030fb34e1ca38f17")


# --- per-surface budget sub-caps ---------------------------------------------

def test_surface_cap_documented_defaults(monkeypatch):
    monkeypatch.delenv("ATLAS_BUDGET_NIGHTLY", raising=False)
    monkeypatch.delenv("ATLAS_BUDGET_ANALYZE", raising=False)
    assert surface_cap_usd("nightly") == 6.00
    assert surface_cap_usd("analyze") == 3.00


def test_surface_cap_env_override(monkeypatch):
    monkeypatch.setenv("ATLAS_BUDGET_ANALYZE", "1.25")
    assert surface_cap_usd("analyze") == 1.25


def test_unknown_surface_without_env_fails_loudly(monkeypatch):
    monkeypatch.delenv("ATLAS_BUDGET_BACKFILL", raising=False)
    with pytest.raises(KeyError):
        surface_cap_usd("backfill")


def test_budget_surface_binds_and_restores():
    assert current_budget_surface() is None
    with budget_surface("nightly"):
        assert current_budget_surface() == "nightly"
        with budget_surface("analyze"):             # inner binding wins…
            assert current_budget_surface() == "analyze"
        assert current_budget_surface() == "nightly"  # …and restores
    assert current_budget_surface() is None


def test_retry_template_hash_matches_file():
    raw = (runner.PROMPTS / RETRY_TEMPLATE_REL_PATH).read_bytes()
    assert load_retry_template()[1] == hashlib.sha256(raw).hexdigest()
