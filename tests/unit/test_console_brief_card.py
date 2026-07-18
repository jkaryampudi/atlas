"""Console TRADING page — the persisted-brief card (ops-reliability build).

Structural checks in the style of test_console_attribution_card.py: the BRIEF
card sits at the TOP of the Trading page (above the approval queue — the
morning read comes before the clicking), its renderer is wired against the
persisted-brief endpoint and joins the shared refresh loop, a 404 renders the
honest empty state, and the prominent flags (billing outage, failed nodes,
expiring proposals, band/CUSUM) are all present. (The whole-<script>
node --check syntax gate already runs in test_console_risk_page.py and covers
this addition.)
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


def test_brief_card_is_at_the_top_of_the_trading_page():
    sec = _trading_section()
    assert 'id="trading-brief"' in sec
    # AT THE TOP: before the fresh-check banner and the approval queue
    assert sec.index('id="trading-brief"') < sec.index("THE FRESH CHECK")
    assert sec.index('id="trading-brief"') < sec.index('id="approval-queue"')


def test_brief_renderer_is_wired_and_honest():
    js = _script()
    assert "/v1/reporting/brief/latest" in js
    assert "function renderTradingBrief(" in js
    renderers = re.search(r"const RENDERERS = \[(.*?)\];", js, re.DOTALL)
    assert renderers and "renderTradingBrief" in renderers.group(1)
    fn = js[js.index("function renderTradingBrief("):]
    fn = fn[:fn.index("/* ---------- live pipeline")]
    # 404 renders the honest empty state, never a fabricated brief
    assert "no persisted brief yet" in fn
    # the prominent flags, all four, plus the countdown marker
    assert "BILLING-OUTAGE SIGNATURE" in fn
    assert "CYCLE NODE FAILED" in fn
    assert "EXPIRING SOON" in fn
    assert "BAND/CUSUM EVENT" in fn
    assert "h left" in fn
    # this card renders the PERSISTED document — it never recomputes
    assert "payload" in fn
