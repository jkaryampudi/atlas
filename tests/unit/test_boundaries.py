"""Architectural boundary enforcement (Doc 07 §2) — the two-plane wall, as a test.

1. dcp/** may not import agents/**   (compute plane never depends on LLMs)
2. agents/** may not import dcp.risk or dcp.execution  (agents cannot reach the veto/broker)
"""
import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "atlas"


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


def test_dcp_never_imports_agents():
    bad = [(f, m) for f, m in _imports("dcp") if m.startswith("atlas.agents")]
    assert not bad, f"compute plane depends on reasoning plane: {bad}"


def test_agents_never_import_risk_or_execution():
    bad = [(f, m) for f, m in _imports("agents")
           if m.startswith(("atlas.dcp.risk", "atlas.dcp.execution"))]
    assert not bad, f"agents can reach the veto/broker: {bad}"
