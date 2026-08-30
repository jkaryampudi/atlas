"""Lessons ledger (learning loop v1): deterministic, templated lesson rows for
notable matured outcomes — learning.lessons (migration 0002).

CLOSED TEMPLATE VOCABULARY — v1 has exactly one live template, and every
lesson row is a fixed template interpolated with enum/numeric fields from
recorded labels. NO free-form or LLM text can reach this table in v1 (the
constitution Article 10 Tier-2 path — prompt refinements, new hypotheses — is
propose-only and does not exist here yet):

- high_conviction_call_failed: a HIGH-conviction directional call (BUY or
  REJECT, non-shadow) graded direction_vindicated = FALSE at a horizon. The
  text says only what the label says — the call was wrong. It does NOT say
  "the dissent was right": dissent_right is defined as NOT vindicated
  (scorecard rule), so that phrase restates the failure as if it were an
  independent finding.

RETIRED 2026-08-30 — specialist_flags_validated ("the desk was warned"): a
specialist seat whose red flags were validated (excess < 0) on a BUY that
failed. Measured over the graded corpus on 2026-08-30: 32 of 32 graded BUYs
carried >= 1 red flag from every seat, and the seats were SUPPORTIVE on 30 of
31 — every failed BUY therefore produced a "warned" lesson regardless of what
the panel actually said. A template that fires on 100% of failures records
noise as lessons; it is retired (no new rows). Existing rows stay (append-only
ledger); the flag_validated label itself is unchanged and still measured by
recalibrate.py. Re-introducing a flags lesson needs a predicate that can be
false (e.g. a CONCERNED stance), registered as a reviewed change.

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
)
# retired templates never come back under the same name (their historical rows
# keep the tag; a re-introduction registers a new, falsifiable predicate)
RETIRED_TEMPLATES: tuple[str, ...] = (
    "specialist_flags_validated",      # 2026-08-30: fired on 32/32 graded BUYs
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
    written labels only. Deterministic: label order preserved. Specialist
    labels are accepted for signature stability but derive nothing since the
    2026-08-30 retirement of specialist_flags_validated (module docstring)."""
    del specialist_labels                  # retired template; labels still measured
    out: list[Lesson] = []
    for ml in memo_labels:
        if (ml.conviction == "HIGH" and ml.recommendation in ("BUY", "REJECT")
                and ml.direction_vindicated is False):
            out.append(Lesson(
                source_type=SOURCE_TYPE, source_id=ml.memo_id,
                lesson=(f"HIGH-conviction {ml.recommendation} on "
                        f"{ml.symbol or '?'} was not vindicated at "
                        f"{ml.horizon_sessions} sessions: excess "
                        f"{_pct(ml.excess)} vs SPY — the call was wrong."),
                tags=("high_conviction_call_failed",
                      f"h{ml.horizon_sessions}", ml.recommendation)))
    return tuple(out)
