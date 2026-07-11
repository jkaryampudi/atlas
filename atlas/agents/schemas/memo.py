"""Agent output schemas — the boundary rule made executable (Constitution 3.1, Doc 01 §3.3).

Validation here is a security control, not a convenience: an agent output that fails
these models is a failed run, full stop.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

# tolerate ordinary prose numerals ("Q3", "2026") but reject price/size/percent shapes
_EXEC_NUMBER = re.compile(
    r"(\$|₹|A\$)\s?\d"            # currency-prefixed
    r"|\d+(\.\d+)?\s?%"           # percentages
    r"|\b\d{1,6}\.\d{2}\b"        # bare price-shaped decimals (172.40)
    r"|\b(target|stop|size|entry)\b.{0,12}\d",
    re.IGNORECASE)


class CommitteeMemo(BaseModel):
    recommendation: Literal["BUY", "WATCHLIST", "REJECT", "INSUFFICIENT_EVIDENCE"]
    conviction: Literal["LOW", "MEDIUM", "HIGH", "N/A"]
    thesis: str
    kill_criteria: list[str]
    evidence_refs: list[str]
    dissent: str

    # context flag injected by the runtime, not the model
    evidence_available: bool = False

    @model_validator(mode="after")
    def constitution_gates(self) -> "CommitteeMemo":
        if self.recommendation == "BUY" and not self.evidence_refs:
            raise ValueError("Constitution 4: BUY without evidence_refs is forbidden")
        if self.recommendation == "BUY" and not self.evidence_available:
            raise ValueError("Constitution 4: BUY forbidden when no DCP evidence attached")
        if not self.evidence_available and self.conviction in ("MEDIUM", "HIGH"):
            raise ValueError("conviction capped at LOW without evidence")
        if self.recommendation != "INSUFFICIENT_EVIDENCE" and len(self.kill_criteria) < 2:
            raise ValueError("Constitution 5: at least two kill criteria required")
        if self.recommendation != "INSUFFICIENT_EVIDENCE" and not self.dissent.strip():
            raise ValueError("Constitution 5: dissent required")
        return self

    @field_validator("thesis", "dissent")
    @classmethod
    def no_execution_numbers(cls, v: str) -> str:
        if _EXEC_NUMBER.search(v):
            raise ValueError("Constitution 3.1: execution-shaped numeric content in narrative")
        return v

    @field_validator("kill_criteria")
    @classmethod
    def kill_criteria_no_exec_numbers(cls, v: list[str]) -> list[str]:
        for item in v:
            if _EXEC_NUMBER.search(item):
                raise ValueError("Constitution 3.1: execution-shaped numeric content in kill criteria")
        return v
