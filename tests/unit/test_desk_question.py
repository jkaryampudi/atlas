"""Desk-review 2026-07 item 10 (desk sliver): the Principal's standing question
is a prompt-store artifact, not a code constant — prompts are code (CLAUDE.md
invariant 5), so its exact text and hash are golden-pinned here; changing the
question is a reviewed diff that must break this pin. live_run.py's inline
copy should converge on the same template file."""
from __future__ import annotations

import hashlib

import atlas.agents.desk as desk
from atlas.agents.runtime.runner import PROMPTS


EXPECTED_QUESTION = ("Given the quant gate verdict and current trend evidence, "
                     "what should the committee do with this name?")
EXPECTED_SHA256 = ("1b00f0feac3675883b2c7920e4b5c79c"
                   "fb09c26974d6baf264dbdf43d81488b4")


def test_question_text_is_byte_identical_to_the_retired_constant():
    text, _ = desk.load_question()
    assert text == EXPECTED_QUESTION


def test_question_template_hash_is_pinned():
    _, sha = desk.load_question()
    assert sha == EXPECTED_SHA256
    raw = (PROMPTS / desk.QUESTION_TEMPLATE_REL_PATH).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256


def test_the_code_constant_is_gone():
    """The question must have exactly one home — the prompt store."""
    assert not hasattr(desk, "QUESTION")
