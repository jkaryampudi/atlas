"""Invariant-6 conformance (CLAUDE.md #6, M48): no module under ``atlas/`` may
read the wall clock directly — every timestamp/date flows through
``atlas.core.clock`` (an injectable ``Clock``), so decisions are replayable and
tests can freeze time. This is the structural sibling of the two-plane-wall test
(``test_boundaries.py``): it walks the AST of the shipped package and fails on any
raw ``datetime.now()`` / ``datetime.utcnow()`` / ``date.today()`` / ``time.time()``
call outside the one sanctioned home, ``atlas/core/clock.py`` (which IS the clock).

The matcher RESOLVES imports before deciding, so it is not fooled by the common
evasions (adversarial review, this cycle): the aliased form
``from datetime import datetime as dt; dt.now()`` and the module-qualified form
``import datetime; datetime.datetime.now()`` are BOTH caught, not only the
``from datetime import datetime`` spelling the codebase happens to use. A
``clock.now()`` / ``SystemClock().now()`` / ``_WALL.now()`` call is not flagged —
its receiver does not resolve to the ``datetime``/``date``/``time`` stdlib."""
from __future__ import annotations

import ast
from pathlib import Path

_ATLAS = Path(__file__).resolve().parents[2] / "atlas"

# The ONLY file allowed to read the real wall clock — it is the injectable clock.
_ALLOWLIST = {"atlas/core/clock.py"}


def _binding_map(tree: ast.AST) -> dict[str, str]:
    """Local name -> canonical origin, resolving aliases and module vs class:
      'M:datetime'/'M:time' — the module   (import datetime [as x])
      'C:datetime'/'C:date' — the class     (from datetime import datetime [as x])
      'F:time'              — the bare time() callable (from time import time)."""
    binding: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "datetime":
                    binding[alias.asname or "datetime"] = "M:datetime"
                elif alias.name == "time":
                    binding[alias.asname or "time"] = "M:time"
        elif isinstance(node, ast.ImportFrom):
            if node.module == "datetime":
                for alias in node.names:
                    if alias.name in ("datetime", "date"):
                        binding[alias.asname or alias.name] = "C:" + alias.name
            elif node.module == "time":
                for alias in node.names:
                    if alias.name == "time":
                        binding[alias.asname or "time"] = "F:time"
    return binding


def _receiver_origin(node: ast.expr, binding: dict[str, str]) -> str | None:
    """Canonical origin of a call receiver expression, or None. Handles both
    ``dt`` (a Name) and ``datetime.datetime`` (module attribute access)."""
    if isinstance(node, ast.Name):
        return binding.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        base = binding.get(node.value.id)
        if base == "M:datetime" and node.attr in ("datetime", "date"):
            return "C:" + node.attr          # datetime.datetime / datetime.date
        if base == "M:time" and node.attr == "time":
            return "M:time"                  # time.time — receiver is the module
    return None


def _wall_clock_violations(tree: ast.AST) -> list[int]:
    """Line numbers of raw wall-clock reads, receiver-resolved so ``clock.now()``
    is never matched and every ``datetime``/``date``/``time`` alias spelling is."""
    binding = _binding_map(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            origin = _receiver_origin(func.value, binding)
            if ((origin == "C:datetime" and func.attr in ("now", "utcnow"))
                    or (origin == "C:date" and func.attr == "today")
                    or (origin == "M:time" and func.attr == "time")):
                hits.append(node.lineno)
        elif isinstance(func, ast.Name) and binding.get(func.id) == "F:time":
            hits.append(node.lineno)         # bare time() (from time import time)
    return hits


def test_no_wall_clock_reads_outside_the_clock_module():
    offenders: list[str] = []
    for path in sorted(_ATLAS.rglob("*.py")):
        rel = path.relative_to(_ATLAS.parent).as_posix()
        tree = ast.parse(path.read_text(), filename=rel)
        for lineno in _wall_clock_violations(tree):
            if rel not in _ALLOWLIST:
                offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "Invariant 6 (injectable time) violated — these sites read the wall clock "
        "directly instead of an injected Clock. Thread a `clock: Clock` (or route "
        "through a module-level SystemClock seam) and call `clock.now()`:\n  "
        + "\n  ".join(offenders))


def test_the_allowlisted_clock_module_is_the_real_source():
    """Guard the guard: clock.py MUST still contain the one sanctioned wall-clock
    read, so the allowlist is load-bearing (not silently dead)."""
    clock_py = _ATLAS / "core" / "clock.py"
    hits = _wall_clock_violations(ast.parse(clock_py.read_text()))
    assert hits, "atlas/core/clock.py no longer reads the wall clock — the " \
                 "SystemClock seam moved; update _ALLOWLIST to match."


def test_matcher_catches_every_evasion_spelling():
    """The matcher is not fooled by import aliasing or module-qualification —
    the exact false-negatives an adversarial review surfaced. Each snippet is a
    genuine wall-clock read that MUST be flagged; the safe seams MUST NOT be."""
    must_flag = [
        "from datetime import datetime\nx = datetime.now(UTC)",           # plain
        "from datetime import datetime as dt\nx = dt.now()",              # aliased class
        "import datetime\nx = datetime.datetime.now(datetime.UTC)",       # module-qualified
        "import datetime as d\nx = d.datetime.utcnow()",                  # aliased module
        "from datetime import date\nx = date.today()",                    # date class
        "from datetime import date as dd\nx = dd.today()",                # aliased date
        "import time\nx = time.time()",                                   # module time
        "from time import time\nx = time()",                              # bare time()
    ]
    for src in must_flag:
        assert _wall_clock_violations(ast.parse(src)), f"MISSED a violation:\n{src}"

    must_not_flag = [
        "x = clock.now()",                          # injected clock — the seam
        "from atlas.core.clock import SystemClock\nx = SystemClock().now()",
        "x = _WALL.now()",                          # module wall-clock seam
        "import time\nx = time.monotonic()",        # a duration, not wall time
        "x = model.today()",                        # unrelated .today on some object
    ]
    for src in must_not_flag:
        assert not _wall_clock_violations(ast.parse(src)), f"FALSE positive:\n{src}"
