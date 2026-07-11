"""Grounded-number verification (ADR-0005 pattern 2; Constitution 3.4 executable).

Every numeric token in an agent's narrative output must appear VERBATIM in the
evidence bodies the output cites. An ungrounded number is fabrication — the run
retries once, then fails closed with an `agent.grounding.failed` audit event.

Whitelisted (never require evidence): rule IDs (L1-L11, DD1-DD3), four-digit
years in prose, and list-index-like small integers inside rule references.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

_NUMERIC = re.compile(r"\d+(?:\.\d+)?")
_WHITELIST = re.compile(
    r"\b(?:L(?:[1-9]|1[01])|DD[1-3])\b"    # risk rule / breaker IDs
    r"|\b(?:19|20)\d{2}\b",                # years in prose
)


def numeric_tokens(text: str) -> list[str]:
    """Numeric tokens requiring evidence grounding (whitelisted spans removed)."""
    cleaned = _WHITELIST.sub(" ", text)
    return _NUMERIC.findall(cleaned)


# reference/ID fields are pointers, not narrative claims — their digits are not
# assertions about the world and never require grounding
_NON_NARRATIVE_FIELDS = {"evidence_refs"}


def _narrative_strings(payload: BaseModel) -> list[str]:
    out: list[str] = []
    for name in type(payload).model_fields:
        if name in _NON_NARRATIVE_FIELDS:
            continue
        value = getattr(payload, name)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, str))
    return out


def grounding_violations(payload: BaseModel,
                         evidence_bodies: dict[str, str]) -> list[str]:
    """Numeric tokens in narrative fields that do not appear verbatim in the
    evidence bodies for the refs the output cites. Empty list = grounded."""
    cited = getattr(payload, "evidence_refs", None)
    if cited is None:
        corpus = " ".join(evidence_bodies.values())
    else:
        corpus = " ".join(evidence_bodies.get(r, "") for r in cited)
    violations = []
    for field_text in _narrative_strings(payload):
        for token in numeric_tokens(field_text):
            if token not in corpus:
                violations.append(f"ungrounded number {token!r} in output "
                                  f"(not verbatim in cited evidence)")
    return violations
