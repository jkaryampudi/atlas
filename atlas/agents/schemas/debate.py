"""Bull/bear debate schemas (ADR-0005 pattern 1). Debate is ADVISORY: nothing
here can open a gate — the CommitteeMemo evidence rules are unchanged."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from atlas.agents.schemas.memo import _EXEC_NUMBER


class DebateCase(BaseModel):
    stance: Literal["BULL", "BEAR"]
    strongest_points: list[str]
    weakest_opposing_point: str
    evidence_refs: list[str]
    concede: str  # one genuine concession — forced intellectual honesty

    # runtime-injected, not model-controlled: a bear that answers as a bull fails
    expected_stance: Literal["BULL", "BEAR", ""] = ""

    @model_validator(mode="after")
    def constitution_gates(self) -> "DebateCase":
        if self.expected_stance and self.stance != self.expected_stance:
            raise ValueError(f"stance {self.stance} does not match assigned "
                             f"{self.expected_stance} role")
        if not 3 <= len(self.strongest_points) <= 5:
            raise ValueError("strongest_points must contain 3-5 items")
        if not self.concede.strip():
            raise ValueError("a genuine concession is required (Constitution 4.3)")
        if not self.weakest_opposing_point.strip():
            raise ValueError("weakest_opposing_point is required")
        return self

    @field_validator("strongest_points")
    @classmethod
    def points_no_exec_numbers(cls, v: list[str]) -> list[str]:
        for item in v:
            if _EXEC_NUMBER.search(item):
                raise ValueError("Constitution 3.1: execution-shaped numeric content "
                                 "in debate points")
        return v

    @field_validator("weakest_opposing_point", "concede")
    @classmethod
    def narrative_no_exec_numbers(cls, v: str) -> str:
        if _EXEC_NUMBER.search(v):
            raise ValueError("Constitution 3.1: execution-shaped numeric content "
                             "in debate narrative")
        return v
