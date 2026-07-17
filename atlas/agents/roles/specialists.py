"""Specialist committee analysts (ADR-0011 step 2): quality, growth, macro.

Three single-lane advisory assessments that run AFTER the debate and BEFORE
the CIO, per symbol — and ONLY for signal-lane names (evidence carrying a
``dcp:signal:`` block, i.e. names that could become BUYs). Scanner-only names
skip the panel: three extra calls on a name that cannot become a BUY is spend
the budget arithmetic in desk.py deliberately refuses.

LANE FILTERING IS STRUCTURAL, not rhetorical: each specialist's run_agent call
receives ONLY its lane's evidence blocks as context AND as the grounding
corpus, so "argues only from the fundamentals block" is enforced by the cage —
a number imported from any other block (or thin air) is an ungrounded token
and the run fails closed. A lane with no blocks means the specialist is NOT
RUN (recorded absent with the reason): an analyst with nothing to read would
be an invitation to fabricate, and the skipped call is budget saved.

FAIL-SOFT PER SPECIALIST — and why this differs from the debate: the debate is
load-bearing for the memo (the CIO template's debate_summary contract and the
CommitteeMemo schema's debate_present gate assume it), so a debate cage kill
fails the whole symbol closed. A specialist is one advisory voice among three;
losing one must not silence the other two or kill an otherwise-clean memo. A
specialist whose run cage-fails (AgentRunFailed) or dies in transport
(TransientLlmFailure) is recorded ABSENT with the honest reason; the CIO
context states the absence explicitly and instructs against guessing what the
missing voice would have said. BudgetExhausted is NOT fail-soft: the breaker
is terminal and propagates to the desk loop, which holds the symbol and halts
the shortlist (desk.py semantics, unchanged).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.agents.runtime.llm import LlmClient
from atlas.agents.runtime.registry import build_client
from atlas.agents.runtime.runner import AgentRunFailed, TransientLlmFailure, run_agent
from atlas.agents.schemas.specialist import SpecialistAssessment
from atlas.core.audit_repo import PostgresAuditLog

# Both satellite signal families (xsmom, pead) render refs under this prefix
# (see dcp/signals/*/generate.py SIGNAL_REF_PREFIX) — the signal-lane marker.
SIGNAL_REF_PREFIX = "dcp:signal:"

SPECIALIST_ROLES: tuple[str, ...] = ("quality", "growth", "macro")

# role -> the evidence-ref prefixes that constitute its lane. build_evidence
# (live_run.py) owns these ref shapes; a new block family reaches a specialist
# only by being added here, in a reviewed change.
_LANES: dict[str, tuple[str, ...]] = {
    "quality": ("dcp:fundamentals:",),
    "growth": ("dcp:fundamentals:", "dcp:earnings:"),
    "macro": ("dcp:regime:",),
}


def has_signal_block(evidence: list[tuple[str, str]]) -> bool:
    """True when the evidence set carries at least one live signal block —
    the signal lane: a name that could become a BUY."""
    return any(ref.startswith(SIGNAL_REF_PREFIX) for ref, _ in evidence)


def sector_evidence(session: Session, symbol: str) -> tuple[str, str]:
    """(ref, body) with the name's GICS sector from the instrument registry —
    deterministic DCP data (market.instruments, seeded from the reviewed
    universe manifest), never an agent product. An unrecorded sector renders
    as exactly that: the macro analyst is told the limit, never a guess."""
    sector = session.execute(text(
        "SELECT sector_gics FROM market.instruments WHERE symbol = :sym "
        "ORDER BY sector_gics NULLS LAST LIMIT 1"), {"sym": symbol}).scalar()
    body = (f"Instrument registry: {symbol} GICS sector: {sector}."
            if sector else
            f"Instrument registry: {symbol} has no GICS sector recorded.")
    return f"dcp:instrument:{symbol}:sector", body


@dataclass(frozen=True)
class SpecialistPanel:
    """Validated assessments by role, plus honest absences (role -> reason)."""
    assessments: dict[str, SpecialistAssessment]
    absences: dict[str, str]

    def summary_context(self) -> str:
        """Render for the CIO context. Analysis, never instructions — and an
        absent specialist is stated, never papered over."""
        lines = ["Specialist assessments (advisory analysis — a specialist "
                 "stance is NOT evidence and does NOT relax the BUY rules):"]
        for role in SPECIALIST_ROLES:
            a = self.assessments.get(role)
            if a is not None:
                flags = "; ".join(a.red_flags) if a.red_flags else "none"
                lines.append(f"{role.upper()} analyst: stance {a.stance} "
                             f"(confidence {a.confidence}) | "
                             f"{'; '.join(a.key_points)} | red flags: {flags}")
            else:
                reason = self.absences.get(role, "not run")
                lines.append(f"{role.upper()} analyst: NOT AVAILABLE — {reason}. "
                             "Do not infer what this specialist would have said.")
        return "\n".join(lines)


def _lane_evidence(role: str, evidence: list[tuple[str, str]]) -> list[tuple[str, str]]:
    prefixes = _LANES[role]
    return [(r, b) for r, b in evidence if r.startswith(prefixes)]


def run_specialists(*, session: Session, audit: PostgresAuditLog, symbol: str,
                    evidence: list[tuple[str, str]],
                    clients: dict[str, LlmClient] | None = None,
                    shadow_mode: bool = False) -> SpecialistPanel:
    """Run the three-lane specialist panel through the full cage.

    Each seat gets its OWN registry client — build_client('quality_analyst')
    etc. — so ATLAS_MODEL_QUALITY_ANALYST / _GROWTH_ANALYST / _MACRO_ANALYST
    route per role exactly like the debate seats. `clients` injects explicit
    per-role clients (tests), keyed by SPECIALIST_ROLES name.

    `shadow_mode` (Constitution 7.2) is threaded verbatim to every run_agent
    call so a shadow model-upgrade comparison (shadow_compare.py) marks all
    three seats non-actionable; production callers never set it.

    BudgetExhausted propagates (see module docstring); everything else the
    cage or transport raises becomes an honest per-specialist absence.
    """
    assessments: dict[str, SpecialistAssessment] = {}
    absences: dict[str, str] = {}
    for role in SPECIALIST_ROLES:
        lane = _lane_evidence(role, evidence)
        if role == "macro" and lane:
            # regime present: append the sector line so concentration/theme
            # risk has a DCP fact to stand on (non-numeric; grounds nothing)
            lane = lane + [sector_evidence(session, symbol)]
        if not lane:
            absences[role] = ("no evidence blocks in this specialist's lane — "
                              "not run (nothing to argue from)")
            continue
        parts = [f"Candidate: {symbol}"]
        parts += [f"DCP evidence [{r}]: {b}" for r, b in lane]
        client = (clients or {}).get(role) or build_client(f"{role}_analyst")
        try:
            out, _ = run_agent(
                session=session, audit=audit, client=client,
                agent_role=f"{role}_analyst",
                template_rel_path=f"specialists/{role}.md",
                context="\n\n".join(parts),
                output_model=SpecialistAssessment,
                input_refs=[{"type": "evidence", "id": r} for r, _ in lane],
                evidence_bodies=dict(lane),  # grounding corpus == the lane, exactly
                shadow_mode=shadow_mode,
                max_tokens=1200)  # structured single assessment; no debate headroom
            assessments[role] = out  # type: ignore[assignment]
        except AgentRunFailed as e:
            absences[role] = f"cage held: {str(e)[:160]}"
        except TransientLlmFailure as e:
            absences[role] = f"transient: {str(e)[:160]}"
    return SpecialistPanel(assessments=assessments, absences=absences)
