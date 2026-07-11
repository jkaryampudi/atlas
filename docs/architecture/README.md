# Atlas AI Capital — Architecture Package v1.0

**An AI-powered investment operating system modelled on a professional hedge fund.**
Hypothetical AUM: A$100,000 · Markets: US & India equities · Long-only, swing/position horizon · Capital preservation first.

Status: **DRAFT FOR APPROVAL — no code until this package is signed off (Principle: design before build).**

---

## Package contents

| # | Document | Purpose |
|---|----------|---------|
| 01 | [Enterprise Architecture](01-enterprise-architecture.md) | System context, two-plane architecture, agent org chart, workflow, technology stack, security & audit model |
| 02 | [Agent Constitution](02-agent-constitution.md) | Binding rules of behaviour for every AI agent: hierarchy, prohibitions, escalation, change control |
| 03 | [Investment Policy Document](03-investment-policy.md) | Mandate, investable universe, strategy definitions, benchmarks, rebalancing, prohibited activities |
| 04 | [Risk Management Policy](04-risk-management-policy.md) | Hard limits, position sizing math, drawdown circuit breakers, FX policy, stress scenarios |
| 05 | [Database Design](05-database-design.md) | PostgreSQL schema: market data, point-in-time fundamentals, event-sourced decision log, trade lifecycle |
| 06 | [API Design](06-api-design.md) | FastAPI surface: resources, state machines, approval endpoints, webhook/event model |
| 07 | [Repository Structure](07-repository-structure.md) | Monorepo layout, module boundaries, testing strategy, CI gates |
| 08 | [Development Roadmap](08-development-roadmap.md) | Seven phases with explicit entry/exit criteria and go/no-go gates |

## The five design decisions that matter most

**1. Two-plane architecture.** All numbers that touch money — signals, position sizes, entry/stop/target prices, risk scores, limit checks — are computed by the **Deterministic Compute Plane**: versioned, unit-tested, reproducible Python. LLM agents live in the **Reasoning Plane**: they interpret, synthesize, challenge, and write memos. An LLM output can *initiate* or *block* a workflow step, but can never *be* a number used in execution. This is the load-bearing wall of explainability and auditability.

**2. Risk veto is code, not conversation.** The Chief Risk Officer *agent* writes risk narratives. The risk *veto* is `RiskEngine.validate(proposal)` — a deterministic function evaluating hard limits from a versioned policy table. No agent, prompt, or human-in-a-hurry can route around it; the execution path structurally requires a passing `risk_check_id`.

**3. Event-sourced decision log.** Every agent run, signal, proposal, approval, and order transition is an immutable append-only event carrying: input snapshot IDs, prompt template hash, model version, output hash. Any historical decision can be reconstructed exactly. "Why did we buy X on 3 March?" is a query, not an archaeology project.

**4. Human approval is a state machine, not a checkbox.** Proposals expire (24h TTL), require explicit per-trade approval on the dashboard, and re-run the risk check at approval time (markets move between proposal and click). Live trading additionally requires a daily arming step.

**5. Funnel economics.** A universe scan is cheap deterministic code; full multi-agent committee treatment is reserved for the handful of candidates that survive the funnel. Target: ≤ 10 full committee runs per day, keeping agent costs proportionate to a A$100k book.

## Challenged assumptions (decisions embedded in this package)

| # | Original assumption | Challenge | Resolution |
|---|--------------------|-----------|------------|
| A1 | AI agents analyse and score trades | LLM-generated numbers are unauditable and unstable | Two-plane split; agents reason over deterministic numbers, never produce them (Doc 01 §3, Doc 02 §4) |
| A2 | CRO agent "has veto power" | An LLM veto can be argued with or injected around | Veto is the deterministic Risk Engine; CRO agent is its narrator and escalation channel (Doc 04 §2) |
| A3 | 5% max position, 20% cash | Forces ≥16 positions on A$100k; cost drag and over-diversification | 5% default retained; documented "small-AUM mode" at 8% cap / 10–14 positions, board-approved parameter (Doc 03 §5, Doc 04 §3) |
| A4 | Direct India equities from day one | AU-resident access to NSE/BSE is restricted (NRI/PIS route, custodial complexity) | Phases 1–5: India exposure via US/AU-listed India ETFs; direct NSE names behind a broker adapter, activated only if account access is secured (Doc 03 §4) |
| A5 | Backtest everything | Free data carries survivorship/restatement bias; PIT fundamentals cost money | Every dataset labelled with a bias class; Validation Agent checklist keys off it; PIT fundamentals a paid Phase 3 dependency (Doc 05 §4, Doc 08 Phase 3) |
| A6 | Full committee reviews opportunities | Continuous agent chatter costs more than a A$100k book earns | Batch daily cadence, scanner-gated funnel, per-day agent budget with a hard cost circuit breaker (Doc 01 §5) |
| A7 | Streamlit dashboard | Fine for internal ops; approval actions need auth + audit | Streamlit retained for Phase 1–5 with an authenticated approval flow through the API (never direct DB writes); revisit at Phase 6 (Doc 06 §6) |

## Reading order

For the fastest complete picture: **01 → 02 → 04 → 03 → 08**, then 05/06/07 when engineering starts.

## Sign-off

| Role | Name | Decision | Date |
|------|------|----------|------|
| Principal / Board | Jay | ☐ Approve ☐ Revise | |

No Phase 1 code is written until this table is signed.
