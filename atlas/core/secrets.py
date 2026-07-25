"""Central secret redaction (F-013).

Provider secrets (the EODHD api_token, the Anthropic key) must never reach logs,
audit-event payloads, exception messages or generated artifacts. EODHD accepts
the token only as a URL query parameter, so the token is unavoidably on the wire;
this module guarantees it is scrubbed the instant a string carrying it would
escape the trusted boundary (an HTTP error, a repr, a log line).
"""
from __future__ import annotations

from collections.abc import Iterable

MASK = "***REDACTED***"


def redact(text: str, *secrets: str | None) -> str:
    """Return ``text`` with every non-empty secret (and its URL-encoded form)
    replaced by a fixed mask. Short/empty secrets are ignored — masking a 1-char
    'secret' would blank the whole string and is never a real credential."""
    out = text
    for s in secrets:
        if not s or len(s) < 6:
            continue
        out = out.replace(s, MASK)
        # api_token travels URL-encoded in some error reprs; cover the common form
        enc = _urlencode_token(s)
        if enc != s:
            out = out.replace(enc, MASK)
    return out


def _urlencode_token(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def redact_all(text: str, secrets: Iterable[str | None]) -> str:
    return redact(text, *secrets)


class RedactingError(RuntimeError):
    """An error whose message is guaranteed free of the given secrets."""

    def __init__(self, message: str, *secrets: str | None) -> None:
        super().__init__(redact(message, *secrets))
