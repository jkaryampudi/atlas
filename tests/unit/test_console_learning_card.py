"""Console RESEARCH page — LEARNING card (learning loop v1). Structural
checks in the style of test_console_attribution_card.py: the card lives on the
Research page, its renderer is wired against /v1/learning/summary and joins
the shared refresh loop, the not-applied honesty is asserted in the UI, and
the empty state is honest — never a fabricated figure. (The whole-<script>
node --check syntax gate in test_console_risk_page.py covers this addition.)
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


def _research_section() -> str:
    html = CONSOLE.read_text()
    m = re.search(r'<section class="view" id="view-research">(.*?)</section>',
                  html, re.DOTALL)
    assert m, "view-research section missing"
    return m.group(1)


def test_research_section_has_the_learning_card():
    sec = _research_section()
    assert 'id="learning"' in sec
    # the honest labelling is part of the card, not decoration
    assert "Measured, Never Applied" in sec
    assert "NOT applied" in sec or "never applied" in sec.lower()
    assert "Principal" in sec


def test_learning_js_is_wired_and_honest():
    js = _script()
    assert "/v1/learning/summary" in js
    assert "function renderLearning(" in js
    renderers = re.search(r"const RENDERERS = \[(.*?)\];", js, re.DOTALL)
    assert renderers and "renderLearning" in renderers.group(1)
    fn = js[js.index("function renderLearning("):]
    fn = fn[:fn.index("async function renderRuns(")]
    # honest empty state — the loop has nothing until an outcome matures
    assert "no matured outcomes labeled yet" in fn
    # the surfacing-only pill is asserted from the API, never assumed
    assert "NOT APPLIED" in fn
    assert "applied===false" in fn.replace(" ", "")
