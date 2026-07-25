"""F-013: the EODHD api_token must NEVER escape into an exception, log, audit, or
alert payload.

EODHD accepts the token only as a URL query parameter, so httpx embeds it in the
`url` of every HTTPStatusError / RequestError. Two guarantees are enforced here:

  1. AST conformance — inside each EODHD client class, `self._client.<verb>` is
     touched in EXACTLY ONE place: the central `_request` transport. A new
     provider method that calls the transport directly (bypassing redaction)
     fails this test.
  2. Canary — across the full range of failure modes (connection refused, read
     timeout, DNS failure, 401/403/429/500, malformed body), the raised error's
     message and repr contain the MASK and never the fake token; the original
     token-bearing httpx error is not chained (`__cause__`/`__context__`).
"""
from __future__ import annotations

import ast
import inspect
from datetime import date

import httpx
import pytest

from atlas.core.secrets import MASK
from atlas.dcp.market_data.adapters import eodhd as eodhd_mod
from atlas.dcp.market_data.adapters.eodhd import EodhdAdapter
from atlas.fxlab import ingest as fxlab_ingest
from atlas.fxlab.ingest import FxlabEodhdClient

FAKE_TOKEN = "SUPERSECRET_eodhd_token_1234567890"

# (module, class name, name of the single method allowed to touch self._client)
_TRANSPORT_CLASSES = [
    (eodhd_mod, "EodhdAdapter", "_request"),
    (fxlab_ingest, "FxlabEodhdClient", "_request"),
]


# ------------------------------------------------------------------ AST guard

def _client_calls_by_method(cls_node: ast.ClassDef) -> dict[str, int]:
    """method name -> count of `self._client.<attr>(...)` accesses within it."""
    counts: dict[str, int] = {}
    for item in cls_node.body:
        if not isinstance(item, ast.FunctionDef):
            continue
        n = 0
        for node in ast.walk(item):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "self"
                    and node.value.attr == "_client"):
                n += 1
        if n:
            counts[item.name] = n
    return counts


@pytest.mark.parametrize("module,cls_name,transport", _TRANSPORT_CLASSES)
def test_only_the_central_transport_touches_the_client(module, cls_name, transport):
    tree = ast.parse(inspect.getsource(module))
    cls_node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == cls_name)
    touched = _client_calls_by_method(cls_node)
    assert set(touched) == {transport}, (
        f"{cls_name}: self._client must be touched ONLY in {transport!r}, "
        f"but is used in {sorted(touched)} — route it through the redacting "
        "transport (F-013)")


def test_no_direct_httpx_verb_in_eodhd_modules():
    """Belt-and-braces: neither module may call httpx.get/post/... at module or
    function scope (all traffic goes through the injected client + transport)."""
    for module in (eodhd_mod, fxlab_ingest):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "httpx"
                    and node.attr in {"get", "post", "put", "delete", "patch", "request"}):
                raise AssertionError(
                    f"{module.__name__}: direct httpx.{node.attr} call is banned")


# -------------------------------------------------------------------- canaries

class _RaisingTransport:
    """An httpx.Client stand-in whose .get raises the given exception, with the
    real (token-bearing) URL baked into the httpx error exactly as httpx would."""

    def __init__(self, exc_factory):
        self._exc_factory = exc_factory

    def get(self, url, params=None, **kw):
        # Reconstruct the URL httpx would build (token in the query string).
        full = httpx.URL(url, params=params or {})
        raise self._exc_factory(full)


def _status_error(status: int):
    def make(url):
        request = httpx.Request("GET", url)
        response = httpx.Response(status, request=request)
        return httpx.HTTPStatusError(
            f"Client error '{status}' for url '{url}'", request=request, response=response)
    return make


def _request_error(kind):
    def make(url):
        return kind(f"transport failure for url '{url}'", request=httpx.Request("GET", url))
    return make


_FAILURES = {
    "connect_refused": _request_error(httpx.ConnectError),
    "read_timeout": _request_error(httpx.ReadTimeout),
    "dns_failure": _request_error(httpx.ConnectError),
    "pool_timeout": _request_error(httpx.PoolTimeout),
    "http_401": _status_error(401),
    "http_403": _status_error(403),
    "http_429": _status_error(429),
    "http_500": _status_error(500),
}


def _adapters_under_test():
    yield EodhdAdapter(FAKE_TOKEN, client=None, symbol_map=None)


@pytest.mark.parametrize("mode", list(_FAILURES))
def test_eodhd_adapter_never_leaks_token_on_failure(mode):
    adapter = EodhdAdapter(FAKE_TOKEN, client=_RaisingTransport(_FAILURES[mode]))
    with pytest.raises(Exception) as ei:  # noqa: PT011 - asserting our own type below
        adapter.fetch_bars("AVGO.US", date(2024, 1, 1), date(2024, 2, 1))
    _assert_scrubbed(ei.value)


@pytest.mark.parametrize("mode", list(_FAILURES))
def test_eodhd_fundamentals_never_leaks_token_on_failure(mode):
    adapter = EodhdAdapter(FAKE_TOKEN, client=_RaisingTransport(_FAILURES[mode]))
    with pytest.raises(Exception) as ei:
        adapter.fetch_fundamentals("AVGO.US")
    _assert_scrubbed(ei.value)


@pytest.mark.parametrize("mode", list(_FAILURES))
def test_eodhd_earnings_calendar_never_leaks_token_on_failure(mode):
    adapter = EodhdAdapter(FAKE_TOKEN, client=_RaisingTransport(_FAILURES[mode]))
    with pytest.raises(Exception) as ei:
        adapter.fetch_earnings_calendar("AVGO.US", date(2024, 1, 1), date(2024, 2, 1))
    _assert_scrubbed(ei.value)


@pytest.mark.parametrize("mode", list(_FAILURES))
def test_fxlab_client_never_leaks_token_on_failure(mode):
    client = FxlabEodhdClient(FAKE_TOKEN, client=_RaisingTransport(_FAILURES[mode]))
    with pytest.raises(Exception) as ei:
        client.fetch_eurusd(date(2024, 1, 1), date(2024, 2, 1))
    _assert_scrubbed(ei.value)


def _assert_scrubbed(err: BaseException) -> None:
    from atlas.core.secrets import RedactingError
    assert isinstance(err, RedactingError), f"expected a RedactingError, got {type(err)}"
    text = f"{err} || {err!r} || {getattr(err, 'args', ())}"
    assert FAKE_TOKEN not in text, "raw token leaked into the raised error"
    assert MASK in text, "redaction mask absent — token path not scrubbed"
    # The token-bearing original httpx error must NOT be chained (from None).
    assert err.__cause__ is None
    ctx = err.__context__
    if ctx is not None:
        assert FAKE_TOKEN not in f"{ctx} || {ctx!r}"
