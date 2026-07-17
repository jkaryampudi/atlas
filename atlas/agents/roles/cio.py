"""CIO Agent role: assembles context, runs, persists the committee memo."""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.roles.debate import DebateResult
from atlas.agents.roles.specialists import SpecialistPanel
from atlas.agents.runtime.llm import LlmClient
from atlas.agents.runtime.runner import run_agent
from atlas.agents.runtime.untrusted import wrap_untrusted
from atlas.agents.schemas.memo import CommitteeMemo
from atlas.core.audit_repo import PostgresAuditLog

# The CIO's generation headroom is a reviewed constant shared with the shadow
# comparison path (shadow_compare.py): the whole point of a shadow run is that
# ONLY the model varies, so the token budget may not silently diverge.
CIO_MAX_TOKENS = 2500  # memo + debate summary need headroom; 1200 truncated live
CIO_TEMPLATE_REL_PATH = "cio/committee_memo.md"


def committee_context(*, symbol: str, question: str,
                      evidence: list[tuple[str, str]],
                      news: list[tuple[str, str]],
                      debate: DebateResult | None,
                      specialists: SpecialistPanel | None) -> str:
    """The exact context string the CIO reads — extracted so the shadow
    model-upgrade comparison (shadow_compare.py) re-assembles a challenger's
    context through THIS function rather than a copy that could drift. Any
    change here changes both the production memo and every future shadow
    comparison identically, which is the isolation the shadow design needs:
    prompts and assembly held constant, only the model varied."""
    parts = [f"Candidate: {symbol}", f"Principal's question: {question}",
             f"evidence_available={'true' if evidence else 'false'}"]
    for ref_id, body in evidence:
        parts.append(f"DCP evidence [{ref_id}]: {body}")
    for src, body in news:
        parts.append(wrap_untrusted(f"news:{src}", body))
    if debate is not None:
        parts.append(debate.summary_context())
    if specialists is not None:
        # rendered AFTER the debate, like the debate: advisory analysis the
        # CIO weighs; absences are stated honestly (ADR-0011 step 2)
        parts.append(specialists.summary_context())
    return "\n\n".join(parts)


def committee_memo(*, session: Session, audit: PostgresAuditLog, client: LlmClient,
                   symbol: str, question: str,
                   evidence: list[tuple[str, str]] | None = None,
                   news: list[tuple[str, str]] | None = None,
                   debate: DebateResult | None = None,
                   specialists: SpecialistPanel | None = None,
                   source: str | None = None) -> CommitteeMemo:
    """`source` (migration 0017) is the external-origin tag for on-demand
    analyses (e.g. 'investing.com'); None = the desk's own work. It is
    persisted to research.memos ONLY and deliberately never appended to the
    prompt context below — a tag that never reaches the model cannot be a
    prompt-injection surface, however hostile the string."""
    evidence = evidence or []
    news = news or []
    context = committee_context(symbol=symbol, question=question,
                                evidence=evidence, news=news,
                                debate=debate, specialists=specialists)
    memo, run_id = run_agent(
        session=session, audit=audit, client=client, agent_role="cio",
        template_rel_path=CIO_TEMPLATE_REL_PATH, context=context,
        output_model=CommitteeMemo,
        input_refs=[{"type": "evidence", "id": r} for r, _ in evidence],
        extra_fields={"evidence_available": bool(evidence),
                      "debate_present": debate is not None},
        evidence_bodies=dict(evidence),  # grounding: numbers must exist in cited refs
        max_tokens=CIO_MAX_TOKENS)
    memo_id = session.execute(text(
        "INSERT INTO research.memos (agent_run_id, memo_type, instrument_symbol, "
        " recommendation, conviction, thesis, kill_criteria, evidence_refs, dissent, "
        " debate_summary, source) "
        "VALUES (:rid, 'committee', :sym, :rec, :conv, :th, CAST(:kc AS jsonb), "
        "        CAST(:er AS jsonb), :d, :ds, :src) RETURNING id"),
        {"rid": run_id, "sym": symbol, "rec": memo.recommendation,
         "conv": memo.conviction, "th": memo.thesis,
         "kc": json.dumps(memo.kill_criteria), "er": json.dumps(memo.evidence_refs),
         "d": memo.dissent, "ds": memo.debate_summary, "src": source}).scalar_one()
    # Provenance (migration 0013): the EXACT evidence text this memo was argued
    # from, verbatim and in order — build_evidence reads live DCP tables, so the
    # bodies are unreconstructible later. Persisted here because this is the one
    # place the memo id exists; part of the same memo-landing transaction that
    # agent.run.completed already evidences on the audit chain.
    if evidence:
        session.execute(text(
            "INSERT INTO research.memo_evidence (memo_id, ordinal, ref, body) "
            "VALUES (:m, :o, :ref, :body)"),
            [{"m": memo_id, "o": i, "ref": ref, "body": body}
             for i, (ref, body) in enumerate(evidence)])
    # Debate provenance (migration 0019, desk-review item 7): the four
    # validated DebateCases persisted VERBATIM (model_dump JSON) with the memo
    # they informed — same transaction, same pattern, same rationale as
    # memo_evidence above: summary_context() is lossy and the cases are
    # unreconstructible later. A cage-failed run never reaches this line, so
    # no memo means no debate rows.
    if debate is not None:
        session.execute(text(
            "INSERT INTO research.memo_debate (memo_id, role, payload) "
            "VALUES (:m, :role, CAST(:p AS jsonb))"),
            [{"m": memo_id, "role": role, "p": json.dumps(case.model_dump())}
             for role, case in (("bull", debate.bull),
                                ("bear", debate.bear),
                                ("bull_rebuttal", debate.bull_rebuttal),
                                ("bear_rebuttal", debate.bear_rebuttal))])
    # Specialist provenance (migration 0025, ADR-0011 step 2): each VALIDATED
    # SpecialistAssessment persisted verbatim with the memo it informed — same
    # transaction, same provenance-not-cache rationale as memo_debate above.
    # An ABSENT specialist (fail-soft: cage kill, transport death, or an empty
    # lane) persists NO row: the table records what the CIO actually read, and
    # the absence itself is already honest in the memo's rendered context and
    # in the failed run's own agent_runs/audit rows. A cage-failed memo never
    # reaches this line, so no memo means no specialist rows.
    if specialists is not None and specialists.assessments:
        session.execute(text(
            "INSERT INTO research.memo_specialists (memo_id, role, payload) "
            "VALUES (:m, :role, CAST(:p AS jsonb))"),
            [{"m": memo_id, "role": role, "p": json.dumps(a.model_dump())}
             for role, a in specialists.assessments.items()])
    return memo
