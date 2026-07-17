"""Shadow model-upgrade comparison (Constitution 7.2; ADR-0005 pattern 4).

THE QUESTION THIS ANSWERS: "should the desk move from the incumbent model to a
challenger?" — answered with EVIDENCE, never with an automatic switch. The
registry change (ATLAS_MODEL_<ROLE> / ATLAS_MODEL_DEFAULT) remains a
Principal-reviewed diff; this module only produces the comparison that review
reads.

WHAT IT DOES. Take the N most recent committee memos that carry full persisted
provenance — verbatim evidence bodies (research.memo_evidence, 0013) and the
four debate cases (research.memo_debate, 0019): the production cohort, the
incumbent's real outputs. For each, reconstruct the evidence context VERBATIM
and re-run the FULL committee path — debate bull/bear + rebuttals, the
specialist panel when the evidence carries a signal block, then the CIO memo —
with EVERY role forced to the challenger model and shadow_mode=True end to
end. Both cohorts are then scored with the SAME deterministic memo-quality
metrics (atlas/agents/evals/metrics.py score_bundle), same thresholds.

ISOLATING THE MODEL VARIABLE. The shadow run uses the same hashed prompt
templates (prompts are code — the runner pins them per run), the same context
assembly (cio.committee_context, run_debate's own builder — shared functions,
not copies), the same max_tokens (CIO_MAX_TOKENS), the same cage (schema
gates, grounding verifier, budget breaker) and the same evidence bytes the
incumbent argued from. The only degree of freedom is the model string.
Two honest residuals, documented rather than hidden: (1) the Principal's
question comes from today's pinned template (question/default.md — the
persisted memo does not record the question text; the template is golden-
pinned, so it differs only if a reviewed change landed since the source memo);
(2) news blocks are not persisted provenance, and the nightly desk passes
none, so the shadow context carries none.

NON-ACTIONABLE, STRUCTURALLY (Constitution 7.2). Shadow outputs land ONLY in
research.shadow_memos (migration 0029) — never research.memos — so nothing
that reads production memos (console, eval --db mode, the future
memo->proposal bridge) can see or act on them; every underlying agent_run is
additionally marked shadow=true (migration 0008). There is deliberately no
"persist to memos" switch on this path.

BUDGET. The whole comparison binds the 'shadow' surface sub-cap
(ATLAS_BUDGET_SHADOW, default $3.00) under the global $10 daily breaker
(runner.py watermark semantics: global always wins) — a comparison must never
starve the nightly desk. BudgetExhausted halts the comparison cleanly:
attempted memo held, remaining memos recorded not-attempted, partial results
reported honestly.

COST ATTRIBUTION, stated honestly: the challenger's per-memo cost is the full
re-run path, attributed by delta on the shadow-run cost tally (this module is
the only shadow writer while it runs). The incumbent's attributable per-memo
cost is the CIO run alone (research.memos.agent_run_id — the schema links no
other run to a memo); its debate/specialist spend is REPORTED AS UNKNOWN,
never estimated. The like-for-like cost comparison is therefore CIO-run vs
CIO-run, with the challenger's full-path figure alongside.

LOCATION (a documented choice): this file lives at atlas/agents/
shadow_compare.py, beside desk.py, because it re-runs the DESK path (debate,
specialists, CIO) and only *consumes* the eval harness for scoring; it also
keeps the orchestrator command exactly
`python -m atlas.agents.shadow_compare --n 8 --model <challenger>`.

CLI:  python -m atlas.agents.shadow_compare --n 8 --model claude-sonnet-5
      (refuses when the challenger equals the incumbent default; writes
      docs/reports/shadow-model-comparison-<date>.md and prints to stdout;
      exit 0 complete, 1 nothing compared, 2 refused, 3 halted-partial)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from atlas.agents.desk import load_question
from atlas.agents.evals.bundles import MemoBundle, _debate
from atlas.agents.evals.metrics import THRESHOLDS, BundleScore, score_bundle
from atlas.agents.evals.run import load_db_bundles
from atlas.agents.roles.cio import CIO_MAX_TOKENS, CIO_TEMPLATE_REL_PATH, committee_context
from atlas.agents.roles.debate import run_debate
from atlas.agents.roles.specialists import SPECIALIST_ROLES, has_signal_block, run_specialists
from atlas.agents.runtime.budget import BudgetExhausted
from atlas.agents.runtime.llm import AnthropicClient, LlmClient
from atlas.agents.runtime.registry import DEFAULT_MODEL, resolve_model
from atlas.agents.runtime.runner import (
    AgentRunFailed,
    TransientLlmFailure,
    budget_surface,
    run_agent,
)
from atlas.agents.schemas.memo import CommitteeMemo
from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock

SHADOW_SURFACE = "shadow"
# Every seat the committee path can occupy — all forced to the challenger.
SHADOW_ROLES: tuple[str, ...] = ("debate_bull", "debate_bear",
                                 "quality_analyst", "growth_analyst",
                                 "macro_analyst", "cio")
REPORTS_DIR = Path(__file__).resolve().parents[2] / "docs" / "reports"

# Challenger output ceilings: 2x production. Empirical (2026-07-18): sonnet-5's
# verbose JSON truncated at the production 2500 on real evidence — 8/8 cage
# holds. Doubling isolates memo QUALITY from ceiling fit; production defaults
# are untouched, and a real switch would raise them in the same reviewed change.
CHALLENGER_MAX_TOKENS_DEBATE = 5000
CHALLENGER_MAX_TOKENS_SPECIALIST = 2400
CHALLENGER_MAX_TOKENS_CIO = CIO_MAX_TOKENS * 2


def incumbent_default() -> str:
    """The desk's default model (registry resolution without a role override)."""
    return os.environ.get("ATLAS_MODEL_DEFAULT") or DEFAULT_MODEL


@dataclass(frozen=True)
class ShadowOutcome:
    """One source memo's shadow re-run result — every outcome recorded, none
    fatal (the desk's fail-soft discipline, desk.py)."""
    source_memo_id: str
    symbol: str
    status: str          # ok | cage_hold | transient | budget_halt | not_attempted
    detail: str = ""


@dataclass(frozen=True)
class ShadowComparison:
    comparison_id: str
    challenger_model: str
    incumbent_models: tuple[tuple[str, str], ...]   # (role, resolved model)
    question_hash: str
    outcomes: tuple[ShadowOutcome, ...]
    incumbent_scores: tuple[BundleScore, ...]
    challenger_scores: tuple[BundleScore, ...]
    incumbent_cio_cost_usd: dict[str, float]        # source memo id -> CIO-run cost
    challenger_cost_usd: dict[str, float]           # source memo id -> full-path cost
    halted: bool


# ---------------------------------------------------------------------------
# Cohort + verbatim evidence reconstruction

def select_cohort(session: Session, *, n_memos: int) -> list[tuple[str, str]]:
    """(memo_id, symbol) for the N most recent committee memos WITH persisted
    evidence bodies AND both debate opening cases — the memos whose full
    committee path can be replayed and whose incumbent side is scoreable on
    every metric. Pure SELECTs."""
    rows = session.execute(text(
        "SELECT CAST(m.id AS text) AS id, COALESCE(m.instrument_symbol,'') AS symbol "
        "FROM research.memos m "
        "WHERE m.memo_type = 'committee' "
        "  AND EXISTS (SELECT 1 FROM research.memo_evidence e WHERE e.memo_id = m.id) "
        "  AND EXISTS (SELECT 1 FROM research.memo_debate d "
        "              WHERE d.memo_id = m.id AND d.role = 'bull') "
        "  AND EXISTS (SELECT 1 FROM research.memo_debate d "
        "              WHERE d.memo_id = m.id AND d.role = 'bear') "
        "ORDER BY m.created_at DESC, m.id LIMIT :n"), {"n": n_memos}).all()
    return [(r.id, r.symbol) for r in rows]


def reconstruct_evidence(session: Session, memo_id: str) -> list[tuple[str, str]]:
    """The EXACT (ref, body) corpus the source memo was argued from —
    research.memo_evidence verbatim, ordinal order, no transformation of any
    kind. Byte equality with what committee_memo persisted is the whole point
    (and is pinned by test)."""
    rows = session.execute(text(
        "SELECT ref, body FROM research.memo_evidence "
        "WHERE CAST(memo_id AS text) = :m ORDER BY ordinal"), {"m": memo_id}).all()
    return [(r.ref, r.body) for r in rows]


def _shadow_spend(session: Session) -> float:
    """Cumulative cost of ALL shadow-marked runs — the attribution tally.
    Delta across one memo's re-run = that memo's full challenger path cost
    (including cage-failed attempts: real spend, honestly counted)."""
    return float(session.execute(text(
        "SELECT COALESCE(SUM(cost_usd),0) FROM research.agent_runs "
        "WHERE shadow")).scalar() or 0)


# ---------------------------------------------------------------------------
# The shadow committee path (one memo)

def _shadow_committee_run(session: Session, audit: PostgresAuditLog, *,
                          symbol: str, question: str, question_hash: str,
                          evidence: list[tuple[str, str]],
                          client: LlmClient) -> dict[str, object]:
    """Full committee path, shadow end to end, on ONE client (every seat is
    the challenger — per-role routing is deliberately bypassed: the model IS
    the variable under test). Returns the payload persisted to shadow_memos.

    CHALLENGER TOKEN HEADROOM: every seat runs at 2x the production output
    ceiling. Empirically required (2026-07-18): sonnet-5's more verbose JSON
    truncated at the production 2500 on real evidence bundles — 8/8 cage
    holds, zero scoreable memos. The comparison isolates memo QUALITY, not
    ceiling fit; a switch decision would raise the production ceilings as
    part of the same reviewed registry change. Production defaults untouched."""
    debate = run_debate(session=session, audit=audit, symbol=symbol,
                        evidence=evidence, bull_client=client,
                        bear_client=client, shadow_mode=True,
                        max_tokens=CHALLENGER_MAX_TOKENS_DEBATE)
    panel = (run_specialists(session=session, audit=audit, symbol=symbol,
                             evidence=evidence,
                             clients={r: client for r in SPECIALIST_ROLES},
                             shadow_mode=True,
                             max_tokens=CHALLENGER_MAX_TOKENS_SPECIALIST)
             if has_signal_block(evidence) else None)
    context = committee_context(symbol=symbol, question=question,
                                evidence=evidence, news=[], debate=debate,
                                specialists=panel)
    memo, cio_run_id = run_agent(
        session=session, audit=audit, client=client, agent_role="cio",
        template_rel_path=CIO_TEMPLATE_REL_PATH, context=context,
        output_model=CommitteeMemo,
        input_refs=[{"type": "evidence", "id": r} for r, _ in evidence],
        extra_fields={"evidence_available": bool(evidence),
                      "debate_present": True},
        evidence_bodies=dict(evidence),
        shadow_mode=True, max_tokens=CHALLENGER_MAX_TOKENS_CIO)
    assert isinstance(memo, CommitteeMemo)
    return {
        "memo": memo.model_dump(),
        "debate": {"bull": debate.bull.model_dump(),
                   "bear": debate.bear.model_dump(),
                   "bull_rebuttal": debate.bull_rebuttal.model_dump(),
                   "bear_rebuttal": debate.bear_rebuttal.model_dump()},
        "specialists": None if panel is None else {
            "assessments": {r: a.model_dump()
                            for r, a in panel.assessments.items()},
            "absences": dict(panel.absences)},
        "cio_run_id": cio_run_id,
        "question_hash": question_hash,
    }


# ---------------------------------------------------------------------------
# Challenger bundles: shadow rows adapted to the harness's bundle shape

def load_shadow_bundles(session: Session, comparison_id: str) -> list[MemoBundle]:
    """research.shadow_memos rows -> MemoBundle, the shape score_bundle reads.
    The evidence corpus is joined back from the SOURCE memo's memo_evidence
    (never duplicated into the payload), so incumbent and challenger are
    scored against identical evidence bytes by construction.
    run_attached_evidence=True: this runner attached the corpus itself."""
    rows = session.execute(text(
        "SELECT CAST(s.source_memo_id AS text) AS source_memo_id, s.payload, "
        "       COALESCE(m.instrument_symbol,'') AS symbol "
        "FROM research.shadow_memos s "
        "JOIN research.memos m ON m.id = s.source_memo_id "
        "WHERE s.comparison_id = :cid ORDER BY s.created_at, s.id"),
        {"cid": comparison_id}).all()
    bundles: list[MemoBundle] = []
    for r in rows:
        payload = r.payload
        memo = payload["memo"]
        evidence = reconstruct_evidence(session, r.source_memo_id)
        bundles.append(MemoBundle(
            bundle_id=f"shadow:{r.source_memo_id}",
            symbol=r.symbol,
            recommendation=str(memo.get("recommendation", "")),
            conviction=str(memo.get("conviction", "")),
            thesis=str(memo.get("thesis", "")),
            kill_criteria=tuple(str(k) for k in memo.get("kill_criteria", [])),
            evidence_refs=tuple(str(x) for x in memo.get("evidence_refs", [])),
            dissent=str(memo.get("dissent", "")),
            debate_summary=str(memo.get("debate_summary", "")),
            evidence=tuple(evidence),
            debate=_debate(payload.get("debate")),
            run_attached_evidence=True))
    return bundles


# ---------------------------------------------------------------------------
# The comparison

def run_shadow_comparison(session: Session, clock: Clock, *, n_memos: int,
                          challenger_model: str,
                          client: LlmClient | None = None) -> ShadowComparison:
    """Re-run the production cohort on the challenger, shadow end to end;
    score both cohorts with the real eval metrics. `client` injects a
    deterministic client (tests — no live calls); default is an Anthropic
    client pinned to the challenger model for every seat.

    Never commits: the session's lifecycle belongs to the caller (the CLI
    commits even on failure so real spend persists — analyze.py precedent)."""
    if challenger_model == incumbent_default():
        raise ValueError(
            f"challenger {challenger_model!r} IS the incumbent default — a "
            "self-comparison isolates nothing; set --model to the candidate")
    cohort = select_cohort(session, n_memos=n_memos)
    comparison_id = (f"shadow-{clock.now():%Y%m%dT%H%M%SZ}-{challenger_model}")
    incumbent_models = tuple((role, resolve_model(role)) for role in SHADOW_ROLES)
    question, question_hash = load_question()
    if cohort and client is None:
        key = os.environ.get("ATLAS_ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError("ATLAS_ANTHROPIC_API_KEY is not set and no client "
                             "was injected — refusing to construct a live "
                             "client that cannot authenticate")
        client = AnthropicClient(key, model=challenger_model)
    audit = PostgresAuditLog(session, clock)
    outcomes: list[ShadowOutcome] = []
    challenger_cost: dict[str, float] = {}
    halted = False
    with budget_surface(SHADOW_SURFACE):
        for i, (memo_id, symbol) in enumerate(cohort):
            assert client is not None
            evidence = reconstruct_evidence(session, memo_id)
            before = _shadow_spend(session)
            try:
                payload = _shadow_committee_run(
                    session, audit, symbol=symbol, question=question,
                    question_hash=question_hash, evidence=evidence,
                    client=client)
            except AgentRunFailed as e:
                challenger_cost[memo_id] = _shadow_spend(session) - before
                outcomes.append(ShadowOutcome(memo_id, symbol, "cage_hold",
                                              str(e)[:200]))
                continue
            except TransientLlmFailure as e:
                challenger_cost[memo_id] = _shadow_spend(session) - before
                outcomes.append(ShadowOutcome(memo_id, symbol, "transient",
                                              str(e)[:180]))
                continue
            except BudgetExhausted as e:
                # terminal for the comparison (desk semantics): hold this
                # memo, halt the rest — attempting them would still spend at
                # the vendor before the check. Partial results stay honest.
                challenger_cost[memo_id] = _shadow_spend(session) - before
                outcomes.append(ShadowOutcome(memo_id, symbol, "budget_halt",
                                              str(e)[:180]))
                outcomes.extend(
                    ShadowOutcome(m, s, "not_attempted",
                                  "budget exhausted — not attempted")
                    for m, s in cohort[i + 1:])
                halted = True
                break
            cost = _shadow_spend(session) - before
            challenger_cost[memo_id] = cost
            payload["cost_usd"] = round(cost, 6)
            session.execute(text(
                "INSERT INTO research.shadow_memos "
                "(source_memo_id, challenger_model, comparison_id, payload, "
                " created_at) "
                "VALUES (CAST(:m AS uuid), :model, :cid, CAST(:p AS jsonb), :ca)"),
                {"m": memo_id, "model": challenger_model, "cid": comparison_id,
                 "p": json.dumps(payload), "ca": clock.now()})
            outcomes.append(ShadowOutcome(memo_id, symbol, "ok"))
    cohort_ids = [m for m, _ in cohort]
    incumbent_scores: tuple[BundleScore, ...] = ()
    incumbent_cio_cost: dict[str, float] = {}
    if cohort_ids:
        incumbent_scores = tuple(
            score_bundle(b) for b in load_db_bundles(
                session, since=None, limit=len(cohort_ids),
                memo_ids=cohort_ids))
        incumbent_cio_cost = {
            r.id: float(r.cost)
            for r in session.execute(text(
                "SELECT CAST(m.id AS text) AS id, "
                "       COALESCE(r.cost_usd, 0) AS cost "
                "FROM research.memos m "
                "LEFT JOIN research.agent_runs r ON r.id = m.agent_run_id "
                "WHERE CAST(m.id AS text) = ANY(:ids)"),
                {"ids": cohort_ids}).all()}
    challenger_scores = tuple(
        score_bundle(b) for b in load_shadow_bundles(session, comparison_id))
    comparison = ShadowComparison(
        comparison_id=comparison_id, challenger_model=challenger_model,
        incumbent_models=incumbent_models, question_hash=question_hash,
        outcomes=tuple(outcomes), incumbent_scores=incumbent_scores,
        challenger_scores=challenger_scores,
        incumbent_cio_cost_usd=incumbent_cio_cost,
        challenger_cost_usd=challenger_cost, halted=halted)
    if cohort:
        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        audit.append(
            event_type="shadow.comparison.completed",
            entity_type="shadow_comparison", entity_id=comparison_id,
            actor_type="human", actor_id="shadow_compare",
            payload={"challenger_model": challenger_model,
                     "n_requested": n_memos, "n_cohort": len(cohort),
                     "outcomes": counts,
                     "challenger_cost_usd": round(sum(challenger_cost.values()), 6),
                     "halted": halted})
    return comparison


# ---------------------------------------------------------------------------
# Reporting

def _cohort_stats(scores: Sequence[BundleScore]) -> dict[str, tuple[str, str]]:
    """metric -> (mean over applicable scores, 'passed/applicable')."""
    out: dict[str, tuple[str, str]] = {}
    for name in THRESHOLDS:
        applicable = [r for s in scores for r in s.results
                      if r.name == name and r.score is not None]
        if not applicable:
            out[name] = ("-", "0/0")
            continue
        mean = (sum((r.score for r in applicable), Decimal(0))
                / Decimal(len(applicable))).quantize(Decimal("0.0001"))
        n_pass = sum(1 for r in applicable if r.passed)
        out[name] = (str(mean), f"{n_pass}/{len(applicable)}")
    return out


def _pass_rate(scores: Sequence[BundleScore]) -> str:
    return f"{sum(1 for s in scores if s.passed)}/{len(scores)}"


def render_report(comp: ShadowComparison) -> str:
    inc_by_id = {s.bundle_id: s for s in comp.incumbent_scores}
    chal_by_id = {s.bundle_id.removeprefix("shadow:"): s
                  for s in comp.challenger_scores}
    inc_stats = _cohort_stats(comp.incumbent_scores)
    chal_stats = _cohort_stats(comp.challenger_scores)
    lines = [
        f"# Shadow model comparison — {comp.comparison_id}",
        "",
        f"challenger: {comp.challenger_model} (EVERY role forced; "
        f"shadow_mode end to end — outputs in research.shadow_memos only)",
        "incumbent (production registry, per role): "
        + ", ".join(f"{r}={m}" for r, m in comp.incumbent_models),
        f"question template hash: {comp.question_hash[:12]} "
        "(same pinned prompts both sides — the model is the only variable)",
        f"cohort: {len(comp.outcomes)} committee memo(s) with persisted "
        "evidence + debate provenance",
        "",
        "## Per-metric (same metrics, same thresholds, both cohorts)",
        "",
        f"{'metric':<24} {'threshold':>9}  {'incumbent mean':>14} "
        f"{'pass':>7}  {'challenger mean':>15} {'pass':>7}",
    ]
    for name, threshold in THRESHOLDS.items():
        im, ip = inc_stats[name]
        cm, cp = chal_stats[name]
        lines.append(f"{name:<24} {str(threshold):>9}  {im:>14} {ip:>7}  "
                     f"{cm:>15} {cp:>7}")
    lines += [
        "",
        f"bundle pass-rate: incumbent {_pass_rate(comp.incumbent_scores)} · "
        f"challenger {_pass_rate(comp.challenger_scores)} scored "
        f"(of {len(comp.outcomes)} attempted)",
        "",
        "## Per-memo side-by-side",
        "",
    ]
    for o in comp.outcomes:
        inc = inc_by_id.get(o.source_memo_id)
        inc_txt = (f"{inc.recommendation} "
                   f"{'PASS' if inc.passed else 'FAIL'}" if inc else "?")
        inc_cost = comp.incumbent_cio_cost_usd.get(o.source_memo_id, 0.0)
        if o.status == "ok":
            chal = chal_by_id.get(o.source_memo_id)
            chal_txt = (f"{chal.recommendation} "
                        f"{'PASS' if chal.passed else 'FAIL'}" if chal else "?")
        else:
            chal_txt = f"{o.status.upper()}: {o.detail}" if o.detail else o.status.upper()
        chal_cost = comp.challenger_cost_usd.get(o.source_memo_id, 0.0)
        lines.append(
            f"[{o.source_memo_id}] {o.symbol}: incumbent {inc_txt} "
            f"(cio run ${inc_cost:.4f}) | challenger {chal_txt} "
            f"(full path ${chal_cost:.4f})")
    total_chal = sum(comp.challenger_cost_usd.values())
    total_inc_cio = sum(comp.incumbent_cio_cost_usd.values())
    lines += [
        "",
        "## Cost",
        "",
        f"challenger full-path total: ${total_chal:.4f} "
        f"({len(comp.challenger_cost_usd)} memo(s), from the shadow run tally; "
        "includes cage-failed attempts — real spend)",
        f"incumbent attributable total: ${total_inc_cio:.4f} — CIO runs ONLY "
        "(research.memos.agent_run_id): the schema links no debate/specialist "
        "run to a memo, so the incumbent's full-path cost is UNKNOWN here, "
        "stated rather than estimated. Like-for-like is CIO-run vs the "
        "challenger's cio seat inside its full-path figure.",
    ]
    if comp.halted:
        lines += [
            "",
            "!! PARTIAL RESULTS: the shadow budget sub-cap "
            "(ATLAS_BUDGET_SHADOW) halted the comparison mid-cohort. "
            "Unattempted memos are listed above; nothing was estimated.",
        ]
    lines += [
        "",
        "## Verdict — read before acting",
        "",
        "The eval harness is a FLOOR-CHECK, not a ranking oracle: a PASS "
        "certifies the deterministic minimums (grounding, observable kill "
        "criteria, non-vacuous dissent, debate diversity, rubric and refs "
        "conformance), not that one model writes better memos than the "
        "other. Quote the pass-rates above as exactly that. A switch "
        "decision ALSO needs (a) the cost delta above and (b) a human read "
        "of a few side-by-side memos from this cohort. NOTHING here switches "
        "any model: adopting the challenger remains a Principal-reviewed "
        "registry change (ATLAS_MODEL_<ROLE>/ATLAS_MODEL_DEFAULT), per "
        "Constitution 7.2 and ADR-0005.",
    ]
    return "\n".join(lines)


def write_report(comp: ShadowComparison, clock: Clock,
                 reports_dir: Path | None = None) -> Path:
    # module attribute resolved at call time so tests can redirect REPORTS_DIR
    reports_dir = reports_dir if reports_dir is not None else REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"shadow-model-comparison-{clock.now():%Y-%m-%d}.md"
    path.write_text(render_report(comp) + "\n")
    return path


# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None, *,
         client: LlmClient | None = None) -> int:
    """`client` is a test seam only (deterministic stub — no live calls in
    tests); the real CLI builds an Anthropic client for the challenger."""
    p = argparse.ArgumentParser(
        description="Shadow model-upgrade comparison (Constitution 7.2): "
                    "re-run recent committee memos on a challenger model, "
                    "non-actionable, and score both cohorts")
    p.add_argument("--n", type=int, default=8,
                   help="cohort size: most recent memos with full provenance")
    p.add_argument("--model", required=True,
                   help="challenger model id (explicit, always)")
    p.add_argument("--database-url", default=None,
                   help="override ATLAS_DATABASE_URL")
    a = p.parse_args(argv)

    if a.model == incumbent_default():
        print(f"REFUSED: --model {a.model!r} is the incumbent default — a "
              "self-comparison isolates nothing", file=sys.stderr)
        return 2
    url = a.database_url or os.environ.get("ATLAS_DATABASE_URL")
    if not url:
        print("--database-url or ATLAS_DATABASE_URL required", file=sys.stderr)
        return 2

    from atlas.core.clock import SystemClock
    clock = SystemClock()
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            try:
                comp = run_shadow_comparison(
                    session, clock, n_memos=a.n,
                    challenger_model=a.model, client=client)
            except ValueError as e:
                print(f"REFUSED: {e}", file=sys.stderr)
                return 2
            except Exception:
                # failed runs' cost + audit trail must persist — the budget
                # breaker counts them (analyze.py precedent); a rollback here
                # would hide real spend
                session.commit()
                raise
            session.commit()
            if not comp.outcomes:
                print("nothing compared: no committee memos with persisted "
                      "evidence + debate provenance — failing closed",
                      file=sys.stderr)
                return 1
            print(render_report(comp))
            path = write_report(comp, clock)
            print(f"\nreport written: {path}")
            return 3 if comp.halted else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
