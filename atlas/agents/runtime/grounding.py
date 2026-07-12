"""Grounded-number verification (ADR-0005 pattern 2; Constitution 3.4 executable).

Every numeric token in an agent's narrative output must appear VERBATIM — as a
STANDALONE numeric token — in the evidence bodies the output cites. An
ungrounded number is fabrication — the run retries once, then fails closed
with an `agent.grounding.failed` audit event.

Token boundaries (desk-review 2026-07 item 4): matching is set-membership over
numeric TOKENS extracted with one shared tokenizer, never substring search
over the corpus. Under substring matching a narrative "20" was grounded by the
"20" inside "SMA20", and small integers leaked out of dates and identifiers —
an ungrounded-number bypass. The tokenizer refuses digits embedded in
alphanumeric identifiers ("SMA20", "v1.2", "16y") on BOTH sides: an identifier
in evidence grounds nothing, and an identifier in the narrative asserts no
numeric claim. This deliberately STRENGTHENS the verifier — narratives that
previously passed via substring accidents now fail closed, which is correct.
Evidence must ground numbers as standalone tokens ("20 sessions", "SMA20
543.21"); the fix for a kill is better evidence, never a weaker verifier.

Whitelisted (never require evidence): rule IDs (L1-L11, DD1-DD3), four-digit
years in prose, and list-index-like small integers inside rule references.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

# A standalone numeric token: digits (optional decimal part) NOT embedded in
# an alphanumeric/underscore identifier and not a fragment of a larger number.
#   - lookbehind: not preceded by [A-Za-z0-9_.] — kills "SMA20"->"20",
#     "v1.2"->"2", and mid-number fragments ("2026"->"026")
#   - lookaheads: not followed by [A-Za-z0-9_] ("16y" is an identifier, not a
#     number) and not by ".<digit>" (prevents "20.5x" degrading to "20" via
#     backtracking); a sentence-ending "543.21." still yields "543.21".
# Punctuation (space , ; : % $ ( ) - = /) is a boundary on both sides, so
# "p=0.830", "(106.5% <= 19476.8%)" and ISO-date components all tokenize.
_NUMERIC = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_])(?!\.\d)")
_WHITELIST = re.compile(
    r"\b(?:L(?:[1-9]|1[01])|DD[1-3])\b"    # risk rule / breaker IDs
    r"|\b(?:19|20)\d{2}\b",                # years in prose
)


def numeric_tokens(text: str) -> list[str]:
    """Numeric tokens requiring evidence grounding (whitelisted spans removed)."""
    cleaned = _WHITELIST.sub(" ", text)
    return _NUMERIC.findall(cleaned)


def corpus_numeric_tokens(text: str) -> frozenset[str]:
    """The set of standalone numeric tokens an evidence corpus can ground —
    the SAME tokenizer as the narrative side, so a digit sequence embedded in
    an identifier ("SMA20") never grounds a bare number. No whitelist
    stripping here: the whitelist governs what a narrative may assert without
    evidence, not what evidence provides."""
    return frozenset(_NUMERIC.findall(text))


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
    grounded = corpus_numeric_tokens(corpus)
    violations = []
    for field_text in _narrative_strings(payload):
        for token in numeric_tokens(field_text):
            if token not in grounded:
                violations.append(f"ungrounded number {token!r} in output "
                                  f"(no standalone verbatim token in cited "
                                  f"evidence)")
    return violations
