"""The 2026-08-25 verdict switch: ATLAS_DESK_ENABLED=0 turns the nightly desk
off with the verdict on the t7 line — never silently, never by missing key.
Analyze-on-demand does not consult this switch (it is user-triggered spend)."""
from __future__ import annotations

from atlas.ops.daily import desk_off_reason


def test_verdict_switch_wins_even_with_a_key(monkeypatch):
    monkeypatch.setenv("ATLAS_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ATLAS_DESK_ENABLED", "0")
    reason = desk_off_reason()
    assert reason is not None and "verdict" in reason
    assert "2026-08-25" in reason          # the decision is named, not vague


def test_no_key_reads_as_no_key(monkeypatch):
    monkeypatch.delenv("ATLAS_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATLAS_DESK_ENABLED", raising=False)
    assert desk_off_reason() == "desk off (no model key configured)"


def test_default_is_on(monkeypatch):
    monkeypatch.setenv("ATLAS_ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ATLAS_DESK_ENABLED", raising=False)
    assert desk_off_reason() is None
