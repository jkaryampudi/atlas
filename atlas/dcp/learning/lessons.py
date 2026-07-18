"""Lessons ledger (learning loop v1): deterministic, templated lesson rows for
notable matured outcomes — learning.lessons (migration 0002).

CLOSED TEMPLATE VOCABULARY — v1 has exactly two templates, and every lesson
row is a fixed template interpolated with enum/numeric fields from recorded
labels. NO free-form or LLM text can reach this table in v1 (the constitution
Article 10 Tier-2 path — prompt refinements, new hypotheses — is propose-only
and does not exist here yet):

- high_conviction_call_failed: a HIGH-conviction directional call (BUY or
  REJECT, non-shadow) graded direction_vindicated = FALSE at a horizon. The
  dissent's verdict is the exact complement of the call's (scorecard rule),
  so "the dissent was right" is derived fact, not commentary.
- specialist_flags_validated: a specialist seat whose red flags were
  validated (excess < 0) on a BUY that failed — the canonical "the desk was
  warned" record, one row per (memo, horizon, seat). By construction this
  fires only for BUYs: flag validation needs excess < 0, and a REJECT with
  excess < 0 is vindicated, which is reinforcement, not a lesson.

Lessons are derived ONLY from newly written labels, in the same transaction
as the labels themselves (labeling.py), so label idempotency IS lesson
idempotency — a matured outcome contributes its lessons exactly once. Rows
are append-only by convention; tags come from the closed sets below; the
numeric field (excess) is formatted deterministically from the stored 6dp
Decimal. source_type='memo_outcome', source_id = the memo's uuid.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:                      # import-cycle guard: labeling imports us
    from atlas.dcp.learning.labeling import MemoLabel, SpecialistLabel

SOURCE_TYPE = "memo_outcome"

# the whole v1 vocabulary — tests pin this; growing it is a reviewed change
LESSON_TEMPLATES: tuple[str, ...] = (
    "high_conviction_call_failed",
    "specialist_flags_validated",
)


@dataclass(frozen=True)
class Lesson:
    source_type: str
    source_id: str                     # memo uuid
    lesson: str
    tags: tuple[str, ...]


def _pct(excess: Decimal) -> str:
    """Deterministic display form of the stored 6dp excess: '+1.25%'/'-8.30%'."""
    return f"{excess:+.2%}"


def derive_lessons(memo_labels: Sequence[MemoLabel],
                   specialist_labels: Sequence[SpecialistLabel],
                   ) -> tuple[Lesson, ...]:
    """The closed derivation (module docstring): templated lessons from newly
    written labels only. Deterministic: label order preserved, memo-level
    lessons before specialist-level for the same memo."""
    by_key = {(ml.memo_id, ml.horizon_sessions): ml for ml in memo_labels}
    out: list[Lesson] = []
    for ml in memo_labels:
        if (ml.conviction == "HIGH" and ml.recommendation in ("BUY", "REJECT")
                and ml.direction_vindicated is False):
            out.append(Lesson(
                source_type=SOURCE_TYPE, source_id=ml.memo_id,
                lesson=(f"HIGH-conviction {ml.recommendation} on "
                        f"{ml.symbol or '?'} was not vindicated at "
                        f"{ml.horizon_sessions} sessions: excess "
                        f"{_pct(ml.excess)} vs SPY — the dissent was right."),
                tags=("high_conviction_call_failed",
                      f"h{ml.horizon_sessions}", ml.recommendation)))
    for sl in specialist_labels:
        mem = by_key.get((sl.memo_id, sl.horizon_sessions))
        if (mem is not None and sl.flag_validated is True
                and mem.recommendation == "BUY"
                and mem.direction_vindicated is False):
            out.append(Lesson(
                source_type=SOURCE_TYPE, source_id=sl.memo_id,
                lesson=(f"{sl.role} specialist flagged {sl.n_red_flags} "
                        f"risk(s) on {mem.symbol or '?'} before the BUY; the "
                        f"call failed at {sl.horizon_sessions} sessions "
                        f"(excess {_pct(mem.excess)} vs SPY) — flags "
                        f"validated."),
                tags=("specialist_flags_validated",
                      f"h{sl.horizon_sessions}", "BUY", sl.role)))
    return tuple(out)
