"""Canonical strategy-lifecycle vocabulary — one source of truth for what a
quant.strategies.state *means* (ADR-0018).

quant.strategies.state is a free-text column under a named CHECK constraint
(migrations 0004 / 0020 / 0035). Historically the tradability gate
`state IN ('paper','live')` was hardcoded at ~12 call sites with no shared
constant (the older SIGNAL_STATES/APPROVED_STATES tuples were defined but
unused). This module names the semantic sets so the ADR-0018 research_shadow
downgrade — the fail-closed sleeve guard (bridge), the promotion gate
(approval), and the non-authoritative performance label (reporting/API) — all
share ONE definition rather than re-deriving it.

The pre-existing SQL literals `state IN ('paper','live')` are intentionally left
in place: they already EXCLUDE research_shadow, so a downgraded strategy stops
deploying capital by construction. This module governs the NEW logic only.
"""
from __future__ import annotations

# States whose signals may deploy real paper-book capital AND be reported as
# validated paper performance — the operational "APPROVED" states.
AUTHORITATIVE_STATES: frozenset[str] = frozenset({"paper", "live"})

# The independent-review downgrade target (ADR-0018): identity and history are
# preserved and observable, but the strategy deploys NO capital and is NEVER
# reported as validated performance.
RESEARCH_SHADOW: str = "research_shadow"

# The only source state from which the fail-closed promotion gate may lift a
# strategy back to 'paper' (mirrors approval.transition_to_paper).
VALIDATED: str = "validated"

# States surfaced in reporting/display (validated OR shadow OR the latching
# demotion target) — so a downgraded strategy is shown and LABELLED, never
# silently hidden from the console/verdicts.
DISPLAY_STATES: frozenset[str] = AUTHORITATIVE_STATES | {RESEARCH_SHADOW, "suspended"}


def is_authoritative(state: str | None) -> bool:
    """True iff a strategy in `state` may deploy paper capital and be reported as
    validated performance. research_shadow / suspended / validated / backtested /
    draft / retired are all NON-authoritative."""
    return state in AUTHORITATIVE_STATES


def is_research_shadow(state: str | None) -> bool:
    """True iff the strategy has been downgraded to non-authoritative shadow
    status by the independent-review process (ADR-0018)."""
    return state == RESEARCH_SHADOW
