"""Outcome labeling (learning loop v1, Constitution Article 10): every matured
scorecard row becomes a learning.outcome_labels row the night it matures, so
the learning plane accumulates a graded corpus from day one.

WHAT IS LABELED — one 'memo' label per matured (memo, horizon) in
research.memo_outcomes (migration 0016), and one 'specialist' label per
(memo, horizon, panel seat) in research.memo_specialists (0025). EVERY tracked
outcome is labeled — HOLD/WATCHLIST and shadow rows included — because the
per-source dartboard baseline needs the full tracked universe (the scorecard's
own rule); ungradable calls carry direction_vindicated NULL, exactly like the
scorecard's vindicated() = None. The scoring rule is NOT restated here:
direction_vindicated calls atlas.dcp.scorecard.vindicated — one rule, one
place.

SPECIALIST ALIGNMENT MAPPING (the one place it lives — specialist_alignment):
a specialist's stance is a directional claim about the CANDIDATE, not about
the CIO's final recommendation (the panel runs before the memo exists, and
its lanes see evidence, not the verdict). It is therefore graded against the
realized excess sign directly, which defines the mapping uniformly for BUY,
REJECT, WATCHLIST, HOLD and every other recommendation:

    stance      excess > 0   excess < 0   excess == 0 (dead heat)
    supportive  aligned      unaligned    unaligned
    concerned   unaligned    aligned      unaligned
    neutral     NULL         NULL         NULL      (no directional claim)

A dead heat confirms no directional claim — the same conservative rule as the
scorecard's "a dead heat vindicates neither direction". Shadow memos get NO
specialist labels (non-actionable end to end, ADR-0005 pattern 4 — the memo
label row still records the outcome with shadow=true).

RED-FLAG VALIDATION (flag_validation): red flags are falsifiable risk
observations about the candidate (schemas/specialist.py). A seat that raised
>= 1 flag is graded flag_validated = TRUE when the candidate underperformed
SPY at the horizon (excess < 0 — the flagged risk materialized), FALSE when
it did not (excess >= 0; the dead heat again confirms nothing), and NULL when
the seat raised no flags (nothing to validate). The task's canonical case — a
red flag on a failed BUY — grades TRUE whenever the BUY failed by actually
underperforming.

MECHANICS: idempotent (a matured outcome labels once per horizon per seat —
planned against the existing-label set, backstopped by the 0030 unique
index + ON CONFLICT DO NOTHING); injectable clock only (labeled_at and the
audit timestamp); append-only by convention; ONE learning.outcomes.labeled
audit event per run when new labels landed, none when zero. Lessons
(lessons.py) are derived from NEWLY written labels in the same transaction,
so label idempotency is lesson idempotency. Pure DCP: deterministic reads of
recorded tables, no agent imports — the specialist payload is read as data
(stance/confidence/red_flags), never through atlas.agents schemas.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.core.audit_repo import PostgresAuditLog
from atlas.core.clock import Clock
from atlas.dcp.learning.lessons import Lesson, derive_lessons
from atlas.dcp.scorecard import HORIZONS, vindicated

# closed stance/confidence vocabularies, mirrored from schemas/specialist.py as
# DATA (the two-plane wall forbids importing agent schemas here); an unknown
# value grades NULL — never a guess
STANCES: tuple[str, ...] = ("supportive", "neutral", "concerned")
CONFIDENCES: tuple[str, ...] = ("low", "medium", "high")


@dataclass(frozen=True)
class MaturedOutcome:
    """One research.memo_outcomes row joined to its memo, as labeling sees it."""
    memo_id: str
    symbol: str | None
    horizon_sessions: int
    excess: Decimal
    recommendation: str | None
    conviction: str | None
    source: str | None
    shadow: bool


@dataclass(frozen=True)
class SpecialistView:
    """One research.memo_specialists row, payload unpacked to what grading needs."""
    memo_id: str
    role: str
    stance: str
    confidence: str
    n_red_flags: int


@dataclass(frozen=True)
class MemoLabel:
    """One 'memo'-kind learning.outcome_labels row."""
    memo_id: str
    symbol: str | None
    horizon_sessions: int
    recommendation: str | None
    conviction: str | None
    source: str | None
    shadow: bool
    excess: Decimal
    direction_vindicated: bool | None


@dataclass(frozen=True)
class SpecialistLabel:
    """One 'specialist'-kind learning.outcome_labels row."""
    memo_id: str
    horizon_sessions: int
    role: str
    stance: str
    confidence: str
    n_red_flags: int
    aligned: bool | None
    flag_validated: bool | None


@dataclass(frozen=True)
class LabelingReport:
    memo_labels: tuple[MemoLabel, ...]
    specialist_labels: tuple[SpecialistLabel, ...]
    lessons: tuple[Lesson, ...]
    already: int                       # matured outcomes previously labeled

    def summary(self) -> str:
        if not self.memo_labels and not self.specialist_labels:
            return "learning: nothing newly matured"
        return (f"learning: +{len(self.memo_labels)} outcome labels "
                f"(+{len(self.specialist_labels)} specialist), "
                f"+{len(self.lessons)} lessons")


def specialist_alignment(stance: str, excess: Decimal) -> bool | None:
    """THE stance mapping (module docstring): a directional stance is aligned
    only when the realized excess sign confirms it; a dead heat confirms no
    directional claim; neutral (or unknown) stances grade NULL."""
    if stance == "supportive":
        return excess > 0
    if stance == "concerned":
        return excess < 0
    return None


def flag_validation(n_red_flags: int, excess: Decimal) -> bool | None:
    """THE red-flag rule (module docstring): flags are validated when the
    flagged candidate underperformed (excess < 0); no flags, nothing to
    validate (NULL); the dead heat validates nothing (FALSE)."""
    if n_red_flags <= 0:
        return None
    return excess < 0


LabelKey = tuple[str, int, str, str]   # (memo_id, horizon, kind, role-or-'')


def plan_labels(
        outcomes: Sequence[MaturedOutcome],
        specialists: Mapping[str, Sequence[SpecialistView]],
        existing: AbstractSet[LabelKey],
) -> tuple[list[MemoLabel], list[SpecialistLabel], int]:
    """Pure planning core: (memo labels to insert, specialist labels to
    insert, already-count). `specialists` maps memo_id -> its panel rows;
    `existing` holds the label keys already recorded — idempotency lives
    here, mirrored by the 0030 unique index. Deterministic: same inputs,
    same plan, outcome order preserved, panel roles sorted."""
    memo_labels: list[MemoLabel] = []
    spec_labels: list[SpecialistLabel] = []
    already = 0
    for o in outcomes:
        if (o.memo_id, o.horizon_sessions, "memo", "") in existing:
            already += 1
        else:
            memo_labels.append(MemoLabel(
                memo_id=o.memo_id, symbol=o.symbol,
                horizon_sessions=o.horizon_sessions,
                recommendation=o.recommendation, conviction=o.conviction,
                source=o.source, shadow=o.shadow, excess=o.excess,
                direction_vindicated=vindicated(o.recommendation, o.excess,
                                                shadow=o.shadow)))
        if o.shadow:
            continue                   # shadow memos: no gradable panel
        panel = sorted(specialists.get(o.memo_id, ()), key=lambda v: v.role)
        for v in panel:
            if (o.memo_id, o.horizon_sessions, "specialist", v.role) in existing:
                continue
            spec_labels.append(SpecialistLabel(
                memo_id=o.memo_id, horizon_sessions=o.horizon_sessions,
                role=v.role, stance=v.stance, confidence=v.confidence,
                n_red_flags=v.n_red_flags,
                aligned=specialist_alignment(v.stance, o.excess),
                flag_validated=flag_validation(v.n_red_flags, o.excess)))
    return memo_labels, spec_labels, already


def _read_matured_outcomes(session: Session) -> list[MaturedOutcome]:
    rows = session.execute(text(
        "SELECT o.memo_id, o.horizon_sessions, o.excess, "
        "       m.instrument_symbol AS symbol, m.recommendation, "
        "       m.conviction, m.source, COALESCE(ar.shadow, false) AS shadow "
        "FROM research.memo_outcomes o "
        "JOIN research.memos m ON m.id = o.memo_id "
        "LEFT JOIN research.agent_runs ar ON ar.id = m.agent_run_id "
        "ORDER BY o.computed_at, o.memo_id, o.horizon_sessions")).all()
    return [MaturedOutcome(
        memo_id=str(r.memo_id), symbol=r.symbol,
        horizon_sessions=int(r.horizon_sessions), excess=Decimal(r.excess),
        recommendation=r.recommendation, conviction=r.conviction,
        source=r.source, shadow=bool(r.shadow)) for r in rows]


def _read_specialists(session: Session) -> dict[str, list[SpecialistView]]:
    """Panel rows for memos that have at least one outcome row. The payload is
    the validated SpecialistAssessment JSON (0025), read as plain data."""
    rows = session.execute(text(
        "SELECT s.memo_id, s.role, s.payload "
        "FROM research.memo_specialists s "
        "WHERE s.memo_id IN (SELECT memo_id FROM research.memo_outcomes) "
        "ORDER BY s.memo_id, s.role")).all()
    out: dict[str, list[SpecialistView]] = {}
    for r in rows:
        p = r.payload or {}
        out.setdefault(str(r.memo_id), []).append(SpecialistView(
            memo_id=str(r.memo_id), role=r.role,
            stance=str(p.get("stance", "")),
            confidence=str(p.get("confidence", "")),
            n_red_flags=len(p.get("red_flags") or [])))
    return out


def _read_existing_keys(session: Session) -> set[LabelKey]:
    rows = session.execute(text(
        "SELECT thesis_memo_id, horizon_sessions, label_kind, "
        "       COALESCE(specialist_role, '') AS role "
        "FROM learning.outcome_labels WHERE label_kind IS NOT NULL")).all()
    return {(str(r.thesis_memo_id), int(r.horizon_sessions), r.label_kind,
             r.role) for r in rows}


def label_matured_outcomes(session: Session, clock: Clock) -> LabelingReport:
    """Label every matured outcome not yet labeled (module docstring), derive
    lessons from the NEW labels, and append ONE learning.outcomes.labeled
    audit event when — and only when — new labels landed."""
    now = clock.now()
    outcomes = _read_matured_outcomes(session)
    if not outcomes:
        return LabelingReport(memo_labels=(), specialist_labels=(),
                              lessons=(), already=0)
    specialists = _read_specialists(session)
    existing = _read_existing_keys(session)
    memo_labels, spec_labels, already = plan_labels(outcomes, specialists,
                                                    existing)

    for ml in memo_labels:
        session.execute(text(
            "INSERT INTO learning.outcome_labels "
            "(thesis_memo_id, label_kind, horizon_sessions, recommendation, "
            " conviction, source, shadow, direction_vindicated, excess, "
            " labeled_at) "
            "VALUES (:m, 'memo', :h, :rec, :conv, :src, :sh, :dv, :ex, :t) "
            "ON CONFLICT DO NOTHING"),
            {"m": ml.memo_id, "h": ml.horizon_sessions,
             "rec": ml.recommendation, "conv": ml.conviction,
             "src": ml.source, "sh": ml.shadow,
             "dv": ml.direction_vindicated, "ex": ml.excess, "t": now})
    for sl in spec_labels:
        session.execute(text(
            "INSERT INTO learning.outcome_labels "
            "(thesis_memo_id, label_kind, horizon_sessions, specialist_role, "
            " specialist_stance, specialist_confidence, n_red_flags, aligned, "
            " flag_validated, labeled_at) "
            "VALUES (:m, 'specialist', :h, :role, :st, :cf, :nf, :al, :fv, :t) "
            "ON CONFLICT DO NOTHING"),
            {"m": sl.memo_id, "h": sl.horizon_sessions, "role": sl.role,
             "st": sl.stance, "cf": sl.confidence, "nf": sl.n_red_flags,
             "al": sl.aligned, "fv": sl.flag_validated, "t": now})

    lessons = derive_lessons(memo_labels, spec_labels)
    for lesson in lessons:
        session.execute(text(
            "INSERT INTO learning.lessons "
            "(source_type, source_id, lesson, tags, created_at) "
            "VALUES (:st, :sid, :l, :tags, :t)"),
            {"st": lesson.source_type, "sid": lesson.source_id,
             "l": lesson.lesson, "tags": list(lesson.tags), "t": now})

    if memo_labels or spec_labels:
        PostgresAuditLog(session, clock).append(
            event_type="learning.outcomes.labeled", entity_type="learning",
            entity_id=now.astimezone(UTC).date().isoformat(),
            actor_type="dcp", actor_id="learning",
            payload={
                "memo_labels": len(memo_labels),
                "specialist_labels": len(spec_labels),
                "lessons": len(lessons),
                "memo_ids": sorted({ml.memo_id for ml in memo_labels}
                                   | {sl.memo_id for sl in spec_labels}),
                "by_horizon": {str(h): sum(1 for ml in memo_labels
                                           if ml.horizon_sessions == h)
                               for h in HORIZONS}})
    return LabelingReport(memo_labels=tuple(memo_labels),
                          specialist_labels=tuple(spec_labels),
                          lessons=lessons, already=already)
