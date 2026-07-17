"""Memo-quality eval runner (desk-review 2026-07 item 8).

Two modes, one metric suite:

FROZEN CORPUS (default) — score every fixture bundle under
atlas/agents/evals/fixtures/ and compare each metric verdict against the
expectations pinned INSIDE the fixture. Exit 0 only when every verdict
matches. This is the pre-deployment tripwire: change a metric, a lexicon, a
threshold — or a fixture — and the corpus run (and the golden-pin test)
breaks loudly before anything reaches production.

    python -m atlas.agents.evals.run
    python -m atlas.agents.evals.run --json /tmp/report.json

REAL MEMOS (--db) — score persisted committee memos read back from
research.memos + memo_evidence + memo_debate. STRICTLY READ-ONLY: the
transaction is set READ ONLY at the database, nothing is written, no audit
events are emitted — this is measurement, not action, and a failing score on
a production memo is information for the Principal, not a breaker. Exit 0
regardless of scores unless --strict is passed; --strict also FAILS CLOSED
(exit 1) when zero memos load — a gate that scored nothing gated nothing.

    python -m atlas.agents.evals.run --db --since 2026-07-01 --limit 50
    python -m atlas.agents.evals.run --db --strict

No clock is used anywhere: reports carry no timestamps, so the same inputs
render byte-identical reports (CLAUDE.md invariant 6 honored by not needing
time at all).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from atlas.agents.evals.bundles import (
    EXPECTED_OVERALL_KEY,
    DebateSideView,
    DebateView,
    MemoBundle,
    load_corpus,
)
from atlas.agents.evals.metrics import THRESHOLDS, BundleScore, score_bundle

_PASS = {True: "PASS", False: "FAIL", None: "n/a"}


# ---------------------------------------------------------------------------
# Reporting

# Documented limits of the deterministic v1 judge, rendered on EVERY report so
# a clean sweep is never read as more than the metrics actually measure
# (eval-harness review 2026-07 findings #12/#14/#15; see metrics.py header).
CAVEATS = (
    "grounding limits inherited verbatim from the production cage BY DESIGN "
    "(word-numbers, date-component grounding, presence-not-attribution); "
    "hardening belongs in atlas/agents/runtime/grounding.py, a reviewed cage "
    "change — the judge never diverges from the cage",
    "rebuttal capitulation is invisible: no metric reads rebuttals "
    "(diversity is measured on opening cases by design); v2 work",
    "closed lexicons under-credit observable events phrased outside them; "
    "widening a lexicon is a reviewed, pin-breaking diff",
    "kill-criterion observability can also be OVER-credited: a pure-vibes "
    "criterion whose mood word sits outside the pinned vibes lexicon "
    "('optimism', 'hype') passes when the sentence carries an incidental "
    "measurable noun plus a trigger verb — a PASS certifies lexicon "
    "conformance, not falsifiability",
    "the debate metric is LEXICAL and order-sensitive: it catches verbatim, "
    "padded and order-preserving interleaved echoes but is blind to "
    "REORDERED partial echo (clause-swapped copies over filler — the pinned "
    "known bypass echo_chamber_reordered) and to semantic/thesaurus echo; "
    "topical boilerplate dissents pass the novelty checks the same way — "
    "that whole class is the deferred LLM judge (shadow-mode first, ADR-0005 "
    "discipline)",
)


def _n_pre_0013(scores: Sequence[BundleScore]) -> int:
    return sum(1 for s in scores
               if any(r.score is None and "migration 0013" in r.detail
                      for r in s.results))


def render_report(scores: Sequence[BundleScore], *, title: str) -> str:
    lines = [f"memo-quality eval — {title} ({len(scores)} bundle(s))",
             "thresholds: " + "  ".join(f"{n}>={t}" for n, t in THRESHOLDS.items()),
             ""]
    for s in scores:
        head = f"[{s.bundle_id}] {s.symbol} {s.recommendation}".rstrip()
        lines.append(f"{head} -> {_PASS[s.passed]}")
        for r in s.results:
            shown = "  -   " if r.score is None else str(r.score)
            lines.append(f"  {r.name:<24} {shown:>7}  {_PASS[r.passed]:<4}  {r.detail}")
        lines.append("")
    n_fail = sum(1 for s in scores if not s.passed)
    lines.append(f"bundles passed: {len(scores) - n_fail}/{len(scores)}")
    n_pre = _n_pre_0013(scores)
    lines.append(f"not scoreable (pre-0013 evidence shape, reported "
                 f"honestly): {n_pre} bundle(s)")
    lines.append("known v1 limits (documented; a PASS is no stronger than "
                 "the metrics):")
    lines.extend(f"  - {c}" for c in CAVEATS)
    return "\n".join(lines)


def report_dict(scores: Sequence[BundleScore]) -> dict[str, object]:
    return {"thresholds": {n: str(t) for n, t in THRESHOLDS.items()},
            "not_scoreable_pre_0013": _n_pre_0013(scores),
            "known_v1_limits": list(CAVEATS),
            "bundles": [
                {"bundle_id": s.bundle_id, "symbol": s.symbol,
                 "recommendation": s.recommendation, "passed": s.passed,
                 "metrics": [
                     {"name": r.name,
                      "score": None if r.score is None else str(r.score),
                      "threshold": str(r.threshold), "passed": r.passed,
                      "detail": r.detail} for r in s.results]}
                for s in scores]}


# ---------------------------------------------------------------------------
# Frozen-corpus mode
def run_corpus() -> tuple[list[BundleScore], list[str]]:
    """Score the frozen corpus; return (scores, expectation mismatches)."""
    mismatches: list[str] = []
    scores: list[BundleScore] = []
    for bundle in load_corpus():
        s = score_bundle(bundle)
        scores.append(s)
        for r in s.results:
            want = bundle.expected.get(r.name, "<missing>")
            if want != r.passed:
                mismatches.append(f"{bundle.bundle_id}.{r.name}: expected "
                                  f"{want}, scored {_PASS.get(r.passed)} "
                                  f"({r.score}) — {r.detail}")
        want_overall = bundle.expected.get(EXPECTED_OVERALL_KEY, "<missing>")
        if want_overall != s.passed:
            mismatches.append(f"{bundle.bundle_id}.overall: expected "
                              f"{want_overall}, scored {s.passed}")
    return scores, mismatches


# ---------------------------------------------------------------------------
# --db mode: read-only over the provenance tables
def _debate_from_rows(rows: dict[str, dict[str, object]]) -> DebateView | None:
    if "bull" not in rows or "bear" not in rows:
        return None                     # predates 0019, or partial: no measure

    def side(role: str) -> DebateSideView | None:
        raw = rows.get(role)
        if raw is None:
            return None
        pts = raw.get("strongest_points", [])
        assert isinstance(pts, list)
        return DebateSideView(
            strongest_points=tuple(str(p) for p in pts),
            weakest_opposing_point=str(raw.get("weakest_opposing_point", "")),
            concede=str(raw.get("concede", "")))

    bull, bear = side("bull"), side("bear")
    assert bull is not None and bear is not None
    return DebateView(bull=bull, bear=bear,
                      bull_rebuttal=side("bull_rebuttal"),
                      bear_rebuttal=side("bear_rebuttal"))


def load_db_bundles(session: Session, *, since: str | None,
                    limit: int,
                    memo_ids: Sequence[str] | None = None) -> list[MemoBundle]:
    """Committee memos as persisted, most recent first. Pure SELECTs.

    `memo_ids` restricts the load to an explicit cohort (shadow model
    comparison: the incumbent side is exactly the memos being re-run) while
    keeping every column mapping in ONE tested place — finding #18 taught
    that a duplicated read path is where a thesis<->dissent swap hides."""
    where = "m.memo_type = 'committee'"
    params: dict[str, object] = {"limit": limit}
    if since is not None:
        where += " AND m.created_at >= CAST(:since AS timestamptz)"
        params["since"] = since
    if memo_ids is not None:
        where += " AND CAST(m.id AS text) = ANY(:ids)"
        params["ids"] = [str(i) for i in memo_ids]
    memo_rows = session.execute(text(
        f"SELECT m.id, m.instrument_symbol, m.recommendation, m.conviction, "
        f"       m.thesis, m.kill_criteria, m.evidence_refs, m.dissent, "
        f"       COALESCE(m.debate_summary, '') AS debate_summary, "
        # did the RUNTIME attach evidence to the producing run? Distinguishes
        # the honest pre-0013 shape (refs persisted, bodies predate the
        # memo_evidence table -> not scoreable) from fabricated refs (nothing
        # attached -> fail closed). finding #16.
        f"       COALESCE(jsonb_array_length(r.input_refs), 0) > 0 "
        f"           AS run_attached_evidence "
        f"FROM research.memos m "
        f"LEFT JOIN research.agent_runs r ON r.id = m.agent_run_id "
        f"WHERE {where} "
        f"ORDER BY m.created_at DESC, m.id LIMIT :limit"), params).all()
    bundles: list[MemoBundle] = []
    for m in memo_rows:
        evidence = session.execute(text(
            "SELECT ref, body FROM research.memo_evidence "
            "WHERE memo_id = :m ORDER BY ordinal"), {"m": m.id}).all()
        debate_rows = {r.role: r.payload for r in session.execute(text(
            "SELECT role, payload FROM research.memo_debate "
            "WHERE memo_id = :m"), {"m": m.id}).all()}
        bundles.append(MemoBundle(
            bundle_id=str(m.id),
            symbol=m.instrument_symbol or "",
            recommendation=m.recommendation or "",
            conviction=m.conviction or "",
            thesis=m.thesis or "",
            kill_criteria=tuple(str(k) for k in (m.kill_criteria or [])),
            evidence_refs=tuple(str(r) for r in (m.evidence_refs or [])),
            dissent=m.dissent or "",
            debate_summary=m.debate_summary,
            evidence=tuple((r.ref, r.body) for r in evidence),
            debate=_debate_from_rows(debate_rows),
            run_attached_evidence=bool(m.run_attached_evidence)))
    return bundles


def run_db(database_url: str, *, since: str | None,
           limit: int) -> list[BundleScore]:
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            # DB-enforced read-only: any write in this transaction — including
            # an accidental future one — raises at the server. No audit
            # events: measurement, not a material action.
            session.execute(text("SET TRANSACTION READ ONLY"))
            bundles = load_db_bundles(session, since=since, limit=limit)
            scores = [score_bundle(b) for b in bundles]
            session.rollback()
        return scores
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Deterministic memo-quality eval (pre-outcome judge)")
    p.add_argument("--db", action="store_true",
                   help="score real persisted committee memos (read-only) "
                        "instead of the frozen fixture corpus")
    p.add_argument("--database-url", default=None,
                   help="override ATLAS_DATABASE_URL for --db mode")
    p.add_argument("--since", default=None,
                   help="--db mode: only memos created at/after this "
                        "ISO timestamp or date")
    p.add_argument("--limit", type=int, default=100,
                   help="--db mode: maximum memos to score (default 100)")
    p.add_argument("--strict", action="store_true",
                   help="--db mode: exit non-zero if any scored memo fails, "
                        "or if zero memos load (fail closed)")
    p.add_argument("--json", dest="json_path", default=None,
                   help="also write the report as JSON to this path")
    a = p.parse_args(argv)

    if a.db:
        url = a.database_url or os.environ.get("ATLAS_DATABASE_URL")
        if not url:
            print("--db requires --database-url or ATLAS_DATABASE_URL",
                  file=sys.stderr)
            return 2
        scores = run_db(url, since=a.since, limit=a.limit)
        print(render_report(scores, title="persisted memos (read-only)"))
        if a.strict and not scores:
            # fail closed (finding #2): a gate that scored nothing gated
            # nothing — an empty result set (mistyped --since, empty or wrong
            # database) must not read as success.
            print("--strict: zero memos scored — failing closed "
                  "(an empty result set is not a passing gate)",
                  file=sys.stderr)
            exit_code = 1
        else:
            exit_code = 1 if (a.strict
                              and any(not s.passed for s in scores)) else 0
    else:
        scores, mismatches = run_corpus()
        print(render_report(scores, title="frozen corpus"))
        if mismatches:
            print("EXPECTATION MISMATCHES (the corpus is a tripwire — a "
                  "metric, threshold, lexicon or fixture changed):")
            for m in mismatches:
                print(f"  {m}")
        else:
            print("all corpus verdicts match pinned expectations")
        exit_code = 1 if mismatches else 0

    if a.json_path:
        Path(a.json_path).write_text(json.dumps(report_dict(scores), indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
