"""CIO Agent role: assembles context, runs, persists the committee memo."""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.roles.debate import DebateResult
from atlas.agents.runtime.llm import LlmClient
from atlas.agents.runtime.runner import run_agent
from atlas.agents.runtime.untrusted import wrap_untrusted
from atlas.agents.schemas.memo import CommitteeMemo
from atlas.core.audit_repo import PostgresAuditLog


def committee_memo(*, session: Session, audit: PostgresAuditLog, client: LlmClient,
                   symbol: str, question: str,
                   evidence: list[tuple[str, str]] | None = None,
                   news: list[tuple[str, str]] | None = None,
                   debate: DebateResult | None = None) -> CommitteeMemo:
    evidence = evidence or []
    news = news or []
    parts = [f"Candidate: {symbol}", f"Principal's question: {question}",
             f"evidence_available={'true' if evidence else 'false'}"]
    for ref_id, body in evidence:
        parts.append(f"DCP evidence [{ref_id}]: {body}")
    for src, body in news:
        parts.append(wrap_untrusted(f"news:{src}", body))
    if debate is not None:
        parts.append(debate.summary_context())
    memo, run_id = run_agent(
        session=session, audit=audit, client=client, agent_role="cio",
        template_rel_path="cio/committee_memo.md", context="\n\n".join(parts),
        output_model=CommitteeMemo,
        input_refs=[{"type": "evidence", "id": r} for r, _ in evidence],
        extra_fields={"evidence_available": bool(evidence),
                      "debate_present": debate is not None},
        evidence_bodies=dict(evidence),  # grounding: numbers must exist in cited refs
        max_tokens=2500)  # memo + debate summary need headroom; 1200 truncated live
    session.execute(text(
        "INSERT INTO research.memos (agent_run_id, memo_type, instrument_symbol, "
        " recommendation, conviction, thesis, kill_criteria, evidence_refs, dissent, "
        " debate_summary) "
        "VALUES (:rid, 'committee', :sym, :rec, :conv, :th, CAST(:kc AS jsonb), "
        "        CAST(:er AS jsonb), :d, :ds)"),
        {"rid": run_id, "sym": symbol, "rec": memo.recommendation,
         "conv": memo.conviction, "th": memo.thesis,
         "kc": json.dumps(memo.kill_criteria), "er": json.dumps(memo.evidence_refs),
         "d": memo.dissent, "ds": memo.debate_summary})
    return memo
