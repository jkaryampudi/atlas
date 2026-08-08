"""Console RESEARCH page — Open BUY Calls card. Structural checks in the
style of test_console_learning_card.py, plus the lesson of the ocShowAll
regression: PR #11 shipped `ocShowAll ? ...` with no declaration, so the
renderer threw ReferenceError on every refresh and the card silently showed
its 'no BUY memos yet' placeholder forever (allSettled isolation swallows the
throw; node --check can't catch a runtime ReferenceError). The declaration
guard below fails on ANY view-state flag that is used but never declared.
"""
from __future__ import annotations

import re
from pathlib import Path

CONSOLE = Path(__file__).parents[2] / "atlas" / "dashboard" / "console.html"


def _script() -> str:
    html = CONSOLE.read_text()
    m = re.search(r"<script>(.*)</script>", html, re.DOTALL)
    assert m, "console.html must contain exactly one inline <script> block"
    return m.group(1)


def test_open_calls_card_and_renderer_are_wired() -> None:
    html = CONSOLE.read_text()
    assert 'id="open-calls"' in html
    js = _script()
    assert "async function renderOpenCalls()" in js
    assert "/v1/research/memos/performance" in js
    assert re.search(r"RENDERERS\s*=\s*\[[^\]]*renderOpenCalls", js, re.DOTALL)


def test_open_calls_shows_gain_spy_and_excess_columns() -> None:
    js = _script()
    assert "GAIN (since memo)" in js
    assert "vs SPY (unrealized)" in js
    for field in ("c.gain", "c.spy", "c.excess"):
        assert field in js, f"renderer must use {field}"


def test_open_calls_show_all_toggle_exists() -> None:
    js = _script()
    assert re.search(r"\blet ocShowAll\b", js)
    assert 'id="oc-more"' in js
    assert "ocShowAll=!ocShowAll" in js.replace(" ", "")


def test_every_view_state_flag_used_is_declared() -> None:
    """The ocShowAll lesson, generalized: any ShowAll/History-style view flag
    referenced anywhere in the script must have a let/const/var declaration —
    an undeclared one is a runtime ReferenceError that kills its panel while
    every other panel keeps working (the worst kind of quiet)."""
    js = _script()
    used = set(re.findall(r"\b(\w+(?:ShowAll|History))\b", js))
    assert used, "expected at least the known view flags"
    for name in sorted(used):
        assert re.search(rf"\b(?:let|const|var)\s+{name}\b", js), (
            f"view flag {name!r} is used but never declared — its panel will "
            "throw ReferenceError on every refresh and stay on its placeholder")
