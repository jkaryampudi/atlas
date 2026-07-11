"""Output schemas for scanner/research/macro/sector agents. Same boundary rules."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from atlas.agents.schemas.memo import _EXEC_NUMBER


def _no_exec_numbers(v: str) -> str:
    if _EXEC_NUMBER.search(v):
        raise ValueError("Constitution 3.1: execution-shaped numeric content")
    return v


class ShortlistItem(BaseModel):
    symbol: str
    signal_ref: str
    rationale: str
    _n = field_validator("rationale")(_no_exec_numbers)


class ScannerShortlist(BaseModel):
    shortlist: list[ShortlistItem]
    excluded_count: int
    # runtime-injected: the only symbols the scanner was shown, and the cap
    allowed_symbols: list[str] = []
    max_candidates: int = 10

    @model_validator(mode="after")
    def funnel_rules(self) -> "ScannerShortlist":
        if len(self.shortlist) > self.max_candidates:
            raise ValueError("funnel cap exceeded (Doc 01 §5)")
        rogue = [i.symbol for i in self.shortlist if i.symbol not in self.allowed_symbols]
        if rogue:
            raise ValueError(f"invented candidates not in screener output: {rogue}")
        return self


class ResearchMemo(BaseModel):
    recommendation: Literal["BUY", "WATCHLIST", "REJECT", "INSUFFICIENT_EVIDENCE"]
    conviction: Literal["LOW", "MEDIUM", "HIGH", "N/A"]
    thesis: str
    business_quality: Literal["WEAK", "ADEQUATE", "STRONG"]
    moat: Literal["NONE", "NARROW", "WIDE"]
    kill_criteria: list[str]
    evidence_refs: list[str]
    dissent: str
    evidence_available: bool = False

    @model_validator(mode="after")
    def gates(self) -> "ResearchMemo":
        if self.recommendation == "BUY" and (not self.evidence_refs or not self.evidence_available):
            raise ValueError("BUY requires DCP evidence (Constitution 4)")
        if not self.evidence_available and self.conviction in ("MEDIUM", "HIGH"):
            raise ValueError("conviction capped at LOW without evidence")
        if self.recommendation != "INSUFFICIENT_EVIDENCE" and len(self.kill_criteria) < 2:
            raise ValueError("at least two kill criteria required")
        return self

    _n1 = field_validator("thesis", "dissent")(_no_exec_numbers)


class SectorTag(BaseModel):
    sector: str
    direction: Literal["TAILWIND", "HEADWIND", "NEUTRAL"]
    why: str


class MacroMemo(BaseModel):
    us_regime: Literal["RISK_ON", "RISK_OFF", "NEUTRAL"]
    india_regime: Literal["RISK_ON", "RISK_OFF", "NEUTRAL"]
    summary: str
    sector_tags: list[SectorTag]
    evidence_refs: list[str]
    dissent: str
    _n = field_validator("summary", "dissent")(_no_exec_numbers)


class SectorNote(BaseModel):
    sector_view: Literal["FAVOURABLE", "MIXED", "UNFAVOURABLE"]
    context: str
    red_flags: list[str]
    evidence_refs: list[str]
    _n = field_validator("context")(_no_exec_numbers)
