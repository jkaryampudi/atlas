"""The Principal's standing question is a hashed prompt template (desk-review
memo 2026-07 item 10c): prompts are code — the default question lives in
atlas/agents/prompts/question/default.md under review, golden-pinned here so
changing it is a visible diff, with the sha256 pin covering the file bytes.

live_run.py and desk.py each carry a small loader for the SAME file (desk.py
belongs to a concurrent workstream; the loader cannot be imported across the
two without a circular import). The convergence test below pins the two
loaders equal so the duplication cannot drift silently."""
from __future__ import annotations

import hashlib
from pathlib import Path

from atlas.agents import desk
from atlas.agents.live_run import QUESTION_TEMPLATE_REL_PATH, load_default_question

PROMPTS = Path(__file__).parents[2] / "atlas" / "agents" / "prompts"

GOLDEN_QUESTION = ("Given the quant gate verdict and current trend evidence, "
                   "what should the committee do with this name?")


def test_default_question_golden_pin():
    question, digest = load_default_question()
    assert question == GOLDEN_QUESTION
    raw = (PROMPTS / QUESTION_TEMPLATE_REL_PATH).read_bytes()
    assert digest == hashlib.sha256(raw).hexdigest()   # hash covers the file bytes


def test_question_contains_no_numbers():
    # the question enters the memo context outside the evidence corpus: a
    # digit here would be an ungrounded-number trap for every memo
    question, _ = load_default_question()
    assert not any(ch.isdigit() for ch in question)


def test_desk_and_live_run_load_the_same_template():
    """Convergence pin (see module docstring): both surfaces must ask the
    committee the same reviewed question, from the same hashed file."""
    if hasattr(desk, "load_question"):            # desk.py's loader (item 10c)
        assert desk.load_question() == load_default_question()
    else:                                          # transitional constant
        assert desk.QUESTION == load_default_question()[0]
