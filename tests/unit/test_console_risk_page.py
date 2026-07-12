"""Console Risk page — structural checks for the what-if pre-flight card and
the L8 heat matrix, plus the whole-<script> syntax gate (node --check): the
console is a single-file pure API client with no build step, so a syntax error
ships silently unless a test parses the script exactly as a browser would.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

CONSOLE = Path(__file__).parents[2] / "atlas" / "dashboard" / "console.html"


def _script() -> str:
    html = CONSOLE.read_text()
    m = re.search(r"<script>(.*)</script>", html, re.DOTALL)
    assert m, "console.html must contain exactly one inline <script> block"
    return m.group(1)


def _risk_section() -> str:
    html = CONSOLE.read_text()
    m = re.search(r'<section class="view" id="view-risk">(.*?)</section>',
                  html, re.DOTALL)
    assert m, "view-risk section missing"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_console_script_passes_node_check(tmp_path):
    js = tmp_path / "console-script.js"
    js.write_text(_script())
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, f"console <script> failed node --check:\n{r.stderr}"


def test_risk_section_has_preflight_card_and_matrix():
    sec = _risk_section()
    for needle in ('id="pf-symbol"', 'id="pf-entry"', 'id="pf-stop"',
                   'id="pf-run"', 'id="pf-result"', 'id="corr-matrix"',
                   'id="corr-note"'):
        assert needle in sec, f"{needle} missing from the Risk section"
    # the advisory label is a hard requirement: a pre-flight must never read
    # as a pre-commitment
    assert "ADVISORY" in sec
    assert "never a pre-commitment" in sec


def test_risk_js_block_is_self_contained():
    js = _script()
    # wired against the new endpoints
    assert "/v1/risk/preflight" in js
    assert "/v1/risk/correlations" in js
    # itemised grid + matrix renderer exist, and the matrix drives its own
    # refresh rather than editing the shared RENDERERS list (concurrency rule)
    assert "function pfGrid(" in js
    assert "function renderCorrMatrix(" in js
    assert "setInterval(renderCorrMatrix, 30000)" in js
    renderers = re.search(r"const RENDERERS = \[(.*?)\];", js, re.DOTALL)
    assert renderers and "renderCorrMatrix" not in renderers.group(1)
    # threshold comes from the live limit-set register, not a hardcode alone
    assert 'x.rule==="L8"' in js and 'x.rule==="L8b"' in js
    # null cells render dim n/a — never a fabricated number
    assert '"n/a"' in js
