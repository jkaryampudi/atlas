"""Specialist committee analyst schema (ADR-0011 step 2).

Three specialists — quality, growth, macro — each argue from ONE lane of the
DCP evidence (the lane filtering is structural, in roles/specialists.py: a
specialist's grounding corpus contains only its lane's blocks, so a number
argued from outside the lane fails the cage). Like the debate, specialist
output is ADVISORY: nothing here can open a gate, and the CommitteeMemo
evidence rules are unchanged.

There is deliberately NO evidence_refs field: the grounding verifier
(runtime/grounding.py) then checks every numeric token against the WHOLE
corpus the runner was given — which, for a specialist, is exactly its lane.
A refs field would let the model narrow its own grounding corpus; a fixed
lane is stricter.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from atlas.agents.schemas.memo import _EXEC_NUMBER


class SpecialistAssessment(BaseModel):
    stance: Literal["supportive", "neutral", "concerned"]
    key_points: list[str]  # 2-4, every numeric token grounded in the lane
    red_flags: list[str]   # 0-3, each a falsifiable observation, never a mood
    confidence: Literal["low", "medium", "high"]

    @model_validator(mode="after")
    def constitution_gates(self) -> "SpecialistAssessment":
        if not 2 <= len(self.key_points) <= 4:
            raise ValueError("key_points must contain 2-4 items")
        if len(self.red_flags) > 3:
            raise ValueError("red_flags must contain at most 3 items")
        if any(not p.strip() for p in self.key_points):
            raise ValueError("blank key point")
        if any(not f.strip() for f in self.red_flags):
            raise ValueError("blank red flag — omit it instead (Constitution 5)")
        return self

    @field_validator("key_points", "red_flags")
    @classmethod
    def no_exec_numbers(cls, v: list[str]) -> list[str]:
        for item in v:
            if _EXEC_NUMBER.search(item):
                raise ValueError("Constitution 3.1: execution-shaped numeric "
                                 "content in specialist output")
        return v
