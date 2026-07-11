# ADR-0005 — TradingAgents pattern adoption under Atlas governance

Date: 2026-07-11 · Status: Accepted · Decider: Principal (Jay)

## Context
TauricResearch/TradingAgents (arXiv 2412.20138) demonstrates a multi-agent trading
organisation whose deliberation quality benefits from structured adversarial debate and
role-specialised models. Atlas already runs a research-agent funnel under a binding
Constitution (Doc 02) with a hard two-plane wall: LLM agents deliberate; only the
Deterministic Compute Plane produces numbers that touch money.

## Decision — adopt four patterns, governed
1. **Bull/bear adversarial debate.** BullResearcher and BearResearcher argue a candidate
   (one case + one rebuttal each, 4 calls, inside the existing budget breaker), each
   forced to concede one genuine point. The CIO memo gains a `debate_summary` and both
   cases as context. Debate output is *advisory evidence* for the CIO — it opens no
   gate: a unanimous debate cannot produce a BUY without DCP evidence refs
   (Constitution 3.1/4.1 unchanged).
2. **Grounded-number verification.** Every numeric token in an agent's narrative output
   must appear verbatim in the evidence bodies it cites (whitelist: rule IDs L1–L11,
   DD1–DD3, years in prose). A violation is a schema-fail: one retry, then fail closed
   with an `agent.grounding.failed` audit event. Constitution 3.4 (no fabricated data)
   made executable.
3. **Resumable workflow checkpoints.** `workflow_runs` + `workflow_node_results` tables;
   each daily-cycle node persists its result before the next runs; re-running a run_id
   skips completed nodes. No node executes twice; every node completion is an audit
   event.
4. **Per-role model registry.** `ATLAS_MODEL_<ROLE>` env resolution with
   `ATLAS_MODEL_DEFAULT` fallback; AnthropicClient and OpenAICompatClient (local models
   via `ATLAS_LOCAL_LLM_URL`); the model string is recorded per run (already in
   `agent_runs.model`), and a `shadow_mode` run flag supports Constitution 7.2 model
   upgrades (logged, marked non-actionable).

## Explicitly REJECTED
- **LLM trader sizing** — violates Constitution 3.1; sizing is Risk Engine code, only.
- **Agent-held approval power** — violates Article 2 (recommend/approve/execute are
  three different hands).
- **Risk-as-LLM-debate** — a risk FAIL is terminal (3.2); risk is code, not rhetoric.
- **Crypto** — outside the investment policy universe (Doc 03).

## DEFERRED
- **Sentiment analyst** — pending an injection-corpus extension covering social-media
  content; social text is the highest-injection-risk evidence class we would ingest
  (Constitution 3.5), and the red-team suite must cover it before any such agent runs.

## Rationale
Their patterns strengthen deliberation; our Constitution keeps deliberation away from
money. Debate improves the quality of what the CIO reads — it must never change what
the CIO is allowed to do.

Signed: Jay, 2026-07-11.
