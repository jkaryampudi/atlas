"""Prompts are code (CLAUDE.md invariant 5): the three specialist templates
(ADR-0011 step 2) and the specialist-aware CIO template are sha256
golden-pinned here over their raw file bytes — editing any of them is a
reviewed change that must break a pin.

The CIO pin records the REVIEWED v2 -> v3 change this build made (one
specialist-assessments bullet + the header version bump; nothing else):
  old raw sha256 4961a9af1cbe13f7cdc8a93bbc2f68bb931629b2ab0ab49e82b7b5c18d712e40
  old composed   b82585423b2146c1d50562461677bc9ec24e33fae8f3e3fd5db007af95b0e495
The composed hash (constitution + template, what research.agent_runs records
as prompt_template_hash) is derived, so pinning the file bytes and the
composition rule pins it too."""
from __future__ import annotations

import hashlib
from pathlib import Path

from atlas.agents.runtime.runner import PROMPTS, load_template

SPECIALIST_TEMPLATE_SHA256 = {
    "specialists/quality.md":
        "efbb9640d0429acf911a51913ef44b7a1642ef54a8f3997992087d4399da491c",
    "specialists/growth.md":
        "547179a4266f1f29ae3c6d99fbf0dc84f74d5dca32dbb8de411b9c1ae89455e6",
    "specialists/macro.md":
        "e3afc803d2e341ad4d4a486c2fbb89fa61194b21d43edbf297ce7f2dfac40e32",
}

CIO_TEMPLATE_SHA256 = (
    "2f0db37ceb95870eb9fbceab2e3070df2673b19ed3f75c6068be457fc6189212")


def _raw(rel: str) -> bytes:
    return (PROMPTS / rel).read_bytes()


def test_specialist_templates_are_golden_pinned():
    actual = {rel: hashlib.sha256(_raw(rel)).hexdigest()
              for rel in SPECIALIST_TEMPLATE_SHA256}
    assert actual == SPECIALIST_TEMPLATE_SHA256


def test_cio_template_pin_records_the_reviewed_specialist_change():
    assert hashlib.sha256(_raw("cio/committee_memo.md")).hexdigest() == \
        CIO_TEMPLATE_SHA256


def test_composed_hash_is_constitution_plus_template():
    """What the runner records per run is sha256(constitution + template);
    pin the composition rule so the raw pins above cover it."""
    constitution = (PROMPTS / "constitution.md").read_text()
    for rel in (*SPECIALIST_TEMPLATE_SHA256, "cio/committee_memo.md"):
        template, t_hash = load_template(rel)
        expected = constitution + "\n\n" + (PROMPTS / rel).read_text()
        assert template == expected
        assert t_hash == hashlib.sha256(expected.encode()).hexdigest()


def test_each_specialist_template_carries_the_shared_discipline():
    """Same numbers-as-data discipline as the debate templates, single-lane
    scope stated, JSON-only response contract present."""
    lanes = {"specialists/quality.md": "fundamentals",
             "specialists/growth.md": "earnings",
             "specialists/macro.md": "regime"}
    for rel, lane_word in lanes.items():
        text = Path(PROMPTS / rel).read_text()
        assert "NUMBER STYLE (Constitution 3.1" in text, rel
        assert "argue ONLY from" in text, rel
        assert lane_word in text, rel
        assert "Respond ONLY with JSON" in text, rel
        assert "advisory analysis" in text, rel
        for key in ("stance", "key_points", "red_flags", "confidence"):
            assert key in text, (rel, key)


def test_cio_template_specialist_addition_is_minimal_and_advisory():
    text = Path(PROMPTS / "cio/committee_memo.md").read_text()
    assert "Specialist assessments (quality/growth/macro)" in text
    assert "NOT AVAILABLE" in text
    # the pre-existing contract survives untouched
    assert "debate_summary:" in text
    assert "BUY is permitted ONLY if" in text
