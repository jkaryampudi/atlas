"""Memo bundles: the unit the harness scores.

A bundle is exactly what the fund persists about one committee memo — the
memo fields (research.memos), the verbatim evidence corpus it was argued from
(research.memo_evidence, migration 0013) and the four verbatim debate cases
that informed it (research.memo_debate, migration 0019). Fixture bundles are
JSON files frozen under atlas/agents/evals/fixtures/; real bundles are read
back from those three tables by run.py's --db mode. Same shape either way,
so the metrics cannot tell a fixture from production.

Fixtures deliberately include bundles that TODAY'S schema gates would reject
(e.g. a directional memo with a blank dissent): the --db mode scores
historical rows that predate the current gates, so the harness must judge
what is persisted, never assume the cage already did.

Two provenance shapes are structurally absent rather than deficient and are
presented honestly (never backfilled, never spuriously failed):
- memos predating migration 0019 have no debate rows -> debate is None;
- memos predating migration 0013 have evidence_refs but no memo_evidence
  bodies. run_attached_evidence (from research.agent_runs.input_refs) proves
  the runtime attached evidence at run time, distinguishing that honest shape
  from a memo that fabricated refs when nothing was attached — the latter
  keeps failing closed (eval-harness review 2026-07 finding #16).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# metric names come from metrics.py; "overall" is the bundle-level verdict
EXPECTED_OVERALL_KEY = "overall"


@dataclass(frozen=True)
class DebateSideView:
    """The narrative surface of one persisted DebateCase (schemas/debate.py) —
    the fields the diversity metric reads. evidence_refs and stance are
    dropped: refs are pointers, and stance is enforced by the cage already."""
    strongest_points: tuple[str, ...]
    weakest_opposing_point: str
    concede: str

    def narrative(self) -> str:
        return " ".join((*self.strongest_points, self.weakest_opposing_point,
                         self.concede))


@dataclass(frozen=True)
class DebateView:
    """The persisted debate as scored. Diversity is measured on the two
    OPENING cases only: rebuttals quote and engage the opposing case by
    design, so opening-case overlap is the honest anchoring measure.

    v1 LIMIT (eval-harness review 2026-07 finding #14, documented, not
    silently accepted): rebuttals are persisted and read back but no metric
    reads them, so a rebuttal that fully capitulates ("the bull is simply
    right, I withdraw the bear case") produces no signal anywhere in the
    harness. A stance-retention metric over rebuttals is deferred to v2 — a
    length floor would not catch capitulation (a fluent surrender is long)
    and would only reward padding."""
    bull: DebateSideView
    bear: DebateSideView
    bull_rebuttal: DebateSideView | None = None
    bear_rebuttal: DebateSideView | None = None


@dataclass(frozen=True)
class MemoBundle:
    bundle_id: str                      # fixture stem, or the memo uuid in --db mode
    symbol: str
    recommendation: str
    conviction: str
    thesis: str
    kill_criteria: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    dissent: str
    debate_summary: str
    evidence: tuple[tuple[str, str], ...]   # (ref, body) in ordinal order
    debate: DebateView | None
    # True when research.agent_runs.input_refs shows the runtime attached
    # evidence to the producing run — with refs present and bodies absent,
    # that is the honest pre-migration-0013 shape (not scoreable), as opposed
    # to fabricated refs (fail closed). Defaults False: fail closed.
    run_attached_evidence: bool = False
    description: str = ""                    # fixtures only: why this bundle exists
    # fixtures only: pinned per-metric expectations (True/False/None) + "overall"
    expected: Mapping[str, bool | None] = field(default_factory=dict)


def _side(raw: Mapping[str, object]) -> DebateSideView:
    points = raw.get("strongest_points", [])
    assert isinstance(points, list)
    return DebateSideView(
        strongest_points=tuple(str(p) for p in points),
        weakest_opposing_point=str(raw.get("weakest_opposing_point", "")),
        concede=str(raw.get("concede", "")))


def _debate(raw: Mapping[str, object] | None) -> DebateView | None:
    if raw is None:
        return None
    bull, bear = raw.get("bull"), raw.get("bear")
    assert isinstance(bull, Mapping) and isinstance(bear, Mapping), \
        "a debate bundle requires both opening cases"
    bull_reb, bear_reb = raw.get("bull_rebuttal"), raw.get("bear_rebuttal")
    return DebateView(
        bull=_side(bull), bear=_side(bear),
        bull_rebuttal=_side(bull_reb) if isinstance(bull_reb, Mapping) else None,
        bear_rebuttal=_side(bear_reb) if isinstance(bear_reb, Mapping) else None)


def load_fixture(path: Path) -> MemoBundle:
    raw = json.loads(path.read_text())
    memo = raw["memo"]
    return MemoBundle(
        bundle_id=path.stem,
        symbol=str(raw.get("symbol", "")),
        recommendation=str(memo.get("recommendation", "")),
        conviction=str(memo.get("conviction", "")),
        thesis=str(memo.get("thesis", "")),
        kill_criteria=tuple(str(k) for k in memo.get("kill_criteria", [])),
        evidence_refs=tuple(str(r) for r in memo.get("evidence_refs", [])),
        dissent=str(memo.get("dissent", "")),
        debate_summary=str(memo.get("debate_summary", "")),
        evidence=tuple((str(r), str(b)) for r, b in raw.get("evidence", [])),
        debate=_debate(raw.get("debate")),
        run_attached_evidence=bool(raw.get("run_attached_evidence", False)),
        description=str(raw.get("description", "")),
        expected=dict(raw.get("expected", {})))


def load_corpus(directory: Path = FIXTURES_DIR) -> tuple[MemoBundle, ...]:
    """The frozen corpus, in deterministic filename order."""
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"no fixture bundles under {directory}")
    return tuple(load_fixture(p) for p in paths)
