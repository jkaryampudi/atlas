"""Scanner, Research, Macro, Sector roles — thin classes on the shared runtime."""
from __future__ import annotations

from sqlalchemy.orm import Session

from atlas.agents.runtime.llm import LlmClient
from atlas.agents.runtime.runner import run_agent
from atlas.agents.runtime.untrusted import wrap_untrusted
from atlas.agents.schemas.roles import MacroMemo, ResearchMemo, ScannerShortlist, SectorNote
from atlas.core.audit_repo import PostgresAuditLog


def scanner_shortlist(*, session: Session, audit: PostgresAuditLog, client: LlmClient,
                      candidates: list[tuple[str, str, str]],  # (symbol, signal_ref, digest)
                      max_candidates: int) -> ScannerShortlist:
    ctx = "\n".join(f"- {s} [{ref}]: {d}" for s, ref, d in candidates)
    out, _ = run_agent(session=session, audit=audit, client=client, agent_role="scanner",
                       template_rel_path="scanner/shortlist.md",
                       context=f"max_candidates={max_candidates}\nScreener candidates:\n{ctx}",
                       output_model=ScannerShortlist,
                       input_refs=[{"type": "signal", "id": r} for _, r, _ in candidates],
                       extra_fields={"allowed_symbols": [s for s, _, _ in candidates],
                                     "max_candidates": max_candidates})
    return out  # type: ignore[return-value]


def research_memo(*, session: Session, audit: PostgresAuditLog, client: LlmClient,
                  symbol: str, evidence: list[tuple[str, str]],
                  news: list[tuple[str, str]] | None = None) -> ResearchMemo:
    parts = [f"Candidate: {symbol}", f"evidence_available={'true' if evidence else 'false'}"]
    parts += [f"DCP evidence [{r}]: {b}" for r, b in evidence]
    parts += [wrap_untrusted(f"news:{src}", b) for src, b in (news or [])]
    out, _ = run_agent(session=session, audit=audit, client=client, agent_role="research",
                       template_rel_path="research/memo.md", context="\n\n".join(parts),
                       output_model=ResearchMemo,
                       input_refs=[{"type": "evidence", "id": r} for r, _ in evidence],
                       extra_fields={"evidence_available": bool(evidence)})
    return out  # type: ignore[return-value]


def macro_regime(*, session: Session, audit: PostgresAuditLog, client: LlmClient,
                 evidence: list[tuple[str, str]]) -> MacroMemo:
    ctx = "\n".join(f"macro series [{r}]: {b}" for r, b in evidence)
    out, _ = run_agent(session=session, audit=audit, client=client, agent_role="macro",
                       template_rel_path="macro/regime.md", context=ctx,
                       output_model=MacroMemo,
                       input_refs=[{"type": "macro", "id": r} for r, _ in evidence])
    return out  # type: ignore[return-value]


def sector_note(*, session: Session, audit: PostgresAuditLog, client: LlmClient,
                sector: str, symbol: str,
                evidence: list[tuple[str, str]]) -> SectorNote:
    ctx = f"Sector: {sector}\nCandidate: {symbol}\n" + \
          "\n".join(f"evidence [{r}]: {b}" for r, b in evidence)
    out, _ = run_agent(session=session, audit=audit, client=client,
                       agent_role=f"sector:{sector}",
                       template_rel_path="sector/note.md", context=ctx,
                       output_model=SectorNote,
                       input_refs=[{"type": "evidence", "id": r} for r, _ in evidence])
    return out  # type: ignore[return-value]
