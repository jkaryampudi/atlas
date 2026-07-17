"""Console TRADING page — sleeve-attribution card (ADR-0012 consequence 4).

Structural checks in the style of test_console_risk_page.py: the card lives on
the Trading page, its renderer is wired against the daily endpoint and joins
the shared refresh loop, and nulls render dim n/a — never a fabricated figure.
(The whole-<script> node --check syntax gate already runs in
test_console_risk_page.py and covers this addition.)
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


def _trading_section() -> str:
    html = CONSOLE.read_text()
    m = re.search(r'<section class="view" id="view-trading">(.*?)</section>',
                  html, re.DOTALL)
    assert m, "view-trading section missing"
    return m.group(1)


def test_trading_section_has_the_attribution_card():
    sec = _trading_section()
    assert 'id="sleeve-attribution"' in sec
    # the honest labelling is part of the card, not decoration
    assert "Core (Beta) vs Satellite (Alpha)" in sec
    assert "flow-adjusted" in sec


def test_attribution_js_is_wired_and_honest():
    js = _script()
    assert "/v1/portfolio/attribution/daily" in js
    assert "function renderSleeveAttribution(" in js
    renderers = re.search(r"const RENDERERS = \[(.*?)\];", js, re.DOTALL)
    assert renderers and "renderSleeveAttribution" in renderers.group(1)
    # null cells render dim n/a — never a fabricated number
    fn = js[js.index("function renderSleeveAttribution("):]
    fn = fn[:fn.index("/* ---------- refresh loop")]
    assert "n/a" in fn
    assert "SATELLITE ALPHA" in fn
    # every sleeve row present, in decomposition order
    assert '"core","xsmom","pead","cash","total"' in fn.replace(" ", "")
