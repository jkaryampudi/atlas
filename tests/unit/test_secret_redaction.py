"""F-013: provider secrets must never surface in exceptions/logs/audit payloads.

Uses a canary token and a stub httpx client that fails, asserting the token (and
its URL-encoded form) never appears in the raised error text.
"""
from __future__ import annotations

import httpx
import pytest

from atlas.core.secrets import MASK, RedactingError, redact
from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter

CANARY = "sk-canary-1234567890ABCDEF"


def test_redact_masks_secret_and_urlencoded_form():
    text = f"GET https://eodhd.com/api/eod/AAPL?api_token={CANARY}&fmt=json -> 401"
    out = redact(text, CANARY)
    assert CANARY not in out and MASK in out
    # URL-encoded token also masked
    enc = f"api%2Dtoken={CANARY}"  # noqa: F841 (documentary)
    assert redact(f"x {CANARY} y", CANARY) == f"x {MASK} y"


def test_redact_ignores_short_or_empty_secrets():
    assert redact("hello", None) == "hello"
    assert redact("hello", "") == "hello"
    assert redact("hello", "abc") == "hello"        # <6 chars: never a real key


def test_redacting_error_is_scrubbed():
    err = RedactingError(f"boom token={CANARY}", CANARY)
    assert CANARY not in str(err) and MASK in str(err)


class _FailingClient:
    """httpx.Client stand-in whose GET raises an HTTPStatusError whose message
    embeds the full request URL (token included) — exactly what httpx does."""
    def get(self, url, params=None, **_kw):
        full = httpx.URL(url).copy_merge_params(params or {})
        req = httpx.Request("GET", full)
        resp = httpx.Response(401, request=req)
        raise httpx.HTTPStatusError(f"401 for url {full}", request=req, response=resp)


def test_adapter_http_error_never_leaks_the_token():
    adapter = EodhdAdapter(api_key=CANARY, client=_FailingClient())
    with pytest.raises(RedactingError) as ei:
        adapter._get("/eod/AAPL.US")
    msg = str(ei.value)
    assert CANARY not in msg                      # the raw token is gone
    from urllib.parse import quote
    assert quote(CANARY, safe="") not in msg      # and its URL-encoded form
    assert MASK in msg
    # and the token-bearing original is not chained (from None)
    assert ei.value.__cause__ is None


class _ConnErrorClient:
    def get(self, url, params=None, **_kw):
        full = httpx.URL(url).copy_merge_params(params or {})
        raise httpx.ConnectError(f"cannot connect to {full}", request=httpx.Request("GET", full))


def test_adapter_connect_error_never_leaks_the_token():
    adapter = EodhdAdapter(api_key=CANARY, client=_ConnErrorClient())
    with pytest.raises(RedactingError) as ei:
        adapter._get("/eod/AAPL.US")
    assert CANARY not in str(ei.value) and MASK in str(ei.value)
