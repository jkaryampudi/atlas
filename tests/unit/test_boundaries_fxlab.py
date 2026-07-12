"""fxlab seal enforcement (ADR-0008 §3), same mechanism as test_boundaries.py:
nothing in atlas/dcp, atlas/agents, atlas/api or atlas/ops may import the
sandbox. The sandbox may import the shared evaluation discipline FROM dcp
(trials are trials); the wall only blocks the other direction."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2] / "atlas"

SEALED_FROM = ("dcp", "agents", "api", "ops")


def _imports(pkg: str) -> list[tuple[str, str]]:
    out = []
    for py in (ROOT / pkg).rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.append((str(py.relative_to(ROOT.parent)), node.module))
            elif isinstance(node, ast.Import):
                out.extend((str(py.relative_to(ROOT.parent)), a.name) for a in node.names)
    return out


@pytest.mark.parametrize("pkg", SEALED_FROM)
def test_fund_planes_never_import_fxlab(pkg):
    bad = [(f, m) for f, m in _imports(pkg) if m.startswith("atlas.fxlab")]
    assert not bad, f"ADR-0008 seal violated — atlas/{pkg} reaches into the sandbox: {bad}"


def test_fxlab_never_imports_the_reasoning_plane():
    """Defence in depth: the sandbox is deterministic compute; it must never
    grow a dependency on the LLM plane either (ADR-0008 §6: no component may
    modify a strategy in response to its own P&L)."""
    bad = [(f, m) for f, m in _imports("fxlab") if m.startswith("atlas.agents")]
    assert not bad, f"sandbox depends on the reasoning plane: {bad}"
