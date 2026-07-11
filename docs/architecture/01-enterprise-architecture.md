# 01 — Enterprise Architecture

Atlas AI Capital · v1.0 · Status: Draft for approval

---

## 1. System context

Atlas is a single-tenant investment operating system managing one portfolio (A$100,000 notional) across US and Indian equity markets. It replicates the *decision discipline* of an institutional hedge fund — separation of duties, documented theses, independent risk, immutable audit — using AI agents for analytical labour and deterministic code for everything that touches money.

```
                 ┌──────────────────────────────────────────────┐
   Market data → │                 ATLAS PLATFORM                │ → Broker API(s)
   News/filings→ │  Compute Plane │ Reasoning Plane │ Audit Core │ → Dashboard (human)
   Macro feeds → │                                              │ → Reports
                 └──────────────────────────────────────────────┘
```

External dependencies:

| Dependency | Purpose | Phase | Notes |
|---|---|---|---|
| EOD price/volume feed (US) | Daily bars, corporate actions | 1 | e.g. EOD Historical Data / Polygon / Tiingo |
| EOD price/volume feed (India) | NSE daily bars | 1 | NSE bhavcopy or vendor; ETFs proxy until direct access |
| Point-in-time fundamentals | Backtest-grade financials | 3 | Paid (e.g. Sharadar SF1 for US); India PIT is a known gap — see Doc 05 §4 |
| News/filings API | Research agent context | 2 | Rate-limited, cached |
| LLM API (Anthropic) | Reasoning plane | 2 | Model version pinned per release |
| Broker API | Paper then live execution | 5–7 | IBKR is the leading candidate: US + India-ETF coverage, paper accounts, FIX/REST |

## 2. Architectural principles (enforced, not aspirational)

1. **No trade without evidence** → a proposal cannot reach `pending_approval` without linked research memo ID, quant signal IDs, and risk check ID (DB foreign keys, not convention).
2. **No strategy without backtesting** → strategies have a lifecycle state machine (`draft → backtested → validated → approved → live → retired`); the signal service only reads `live` strategies.
3. **No execution without risk approval** → `orders.risk_check_id NOT NULL` + service-layer guard + the Execution service holds the *only* broker credentials.
4. **No agent overrides risk** → agents have zero write access to `risk_limits`; limit changes go through a human-only change-control endpoint with dual confirmation.
5. **Explainable** → every number traces to a versioned function + input snapshot; every narrative traces to a prompt hash + model version.
6. **Auditable** → append-only event log; no UPDATE/DELETE grants on audit tables at the Postgres role level.
7. **Human before live** → per-trade approval + daily system arming for live mode.

## 3. The two-plane architecture

This is the core structural decision of the platform.

### 3.1 Deterministic Compute Plane (DCP)

Pure Python, fully unit-tested, versioned, reproducible. Owns every number.

| Service | Responsibility |
|---|---|
| `market_data` | Ingestion, adjustment (splits/dividends), validation, gap detection |
| `indicators` | MAs, relative strength, volatility, volume metrics — pure functions over bars |
| `signal_engine` | Runs approved strategies over the universe; emits scored signals |
| `screener` | Universe filters (liquidity, market cap, data quality) — the top of the funnel |
| `backtester` | Event-driven backtests with realistic costs, PIT data awareness |
| `risk_engine` | Position sizing, limit validation, exposure/correlation math, drawdown tracking, stress calc |
| `portfolio_engine` | Positions, P&L, attribution math, FX translation |
| `execution` | Broker adapter, order state machine, reconciliation — the only service with broker credentials |

Properties: same inputs → same outputs, forever. Every output row records `code_version` (git SHA) and input snapshot IDs.

### 3.2 Reasoning Plane (LLM agents)

Agents consume DCP outputs + retrieved documents and produce **structured narratives and recommendations** — never numbers destined for execution.

| Agent | Consumes | Produces |
|---|---|---|
| Market Scanner Agent | Screener output, signal digests | Ranked shortlist with one-line rationale (caps the funnel) |
| Equity Research Analyst | Fundamentals, filings, news, DCP valuation metrics | Research memo: business quality, moat, management, valuation *interpretation*, thesis, kill criteria |
| Macro Economist | Rates, inflation prints, FX, RBI/Fed statements | Macro regime memo (US & India), tailwind/headwind tags per sector |
| Sector Specialists (US: Tech, Health, Fin, Consumer, Energy; IN: Banking, IT Svcs, Mfg, Consumer) | Sector data + candidate memos | Sector context, competitive dynamics, red flags |
| Quant Research Agent | Backtest results, market structure notes | Strategy *hypotheses* (specs for the DCP to implement), pattern commentary |
| Quant Validation Agent | Backtest artifacts, data bias labels | Adversarial validation report: overfitting, biases, OOS integrity — approve/reject with reasons |
| CIO Agent | All memos + signals + risk report | Investment Committee Memo: recommend / reject / request-more-work, with explicit disagreement log |
| CRO Agent | Risk engine outputs, stress results | Risk narrative, escalations, limit-utilisation commentary (the *veto itself* is the risk engine) |
| Stress Testing Agent | Scenario library + portfolio | Scenario selection rationale, plain-English impact summary (scenario *math* is DCP) |
| Portfolio Manager Agent | Holdings, theses, exit conditions, price action | Daily holding review: thesis intact? add/reduce/hold/exit recommendation |
| Performance Attribution Agent | Attribution math from DCP | Monthly narrative: what worked, what failed, decision-quality vs luck |
| Trader Agent | Approved proposal, liquidity metrics | Order strategy note (limit vs market band, participation guidance) — final order params are DCP-clamped |
| Compliance Agent | Event log, limit history, agent runs | Exception reports, rule-drift alerts, audit summaries |

### 3.3 The boundary rule

An agent output may contain numbers *quoted from* DCP inputs (traceable by reference ID). If an agent emits a novel number in a field that feeds execution, the orchestrator rejects the output at schema validation. Structured agent outputs use enums and references, not free numerics: e.g. `recommendation: BUY|HOLD|REJECT`, `conviction: LOW|MEDIUM|HIGH`, `signal_refs: [uuid]`.

## 4. Organisational model and authority map

```
                        HUMAN PRINCIPAL (final authority)
                               │ approves trades, arms live mode,
                               │ changes risk limits, approves strategies
        ┌──────────────────────┼───────────────────────┐
   CIO Agent              RISK ENGINE (code)       Compliance Agent
   (recommends)           + CRO Agent (narrates)   (observes everything,
        │                      │  hard veto          reports to human only)
 ┌──────┴────────┐             │
 Research Dept   Quant Dept    Stress Agent
 (analyst, macro,(research +
  sector specs)   validation)
        │
   Portfolio Mgmt (PM + Attribution)
        │
   Trading Ops (Trader + Execution service)
```

Authority rules: recommendations flow **up**, never sideways into execution. Compliance reports only to the human. The Execution service accepts input from exactly one source: an `approved` proposal record with a valid risk check.

## 5. Trade decision workflow (daily batch)

Cadence: once per market day per region, after data ingestion completes. Not continuous — see funnel economics.

```
T0  Market Data Update (DCP)        universe: ~600 US / ~50 India-ETF+names
     ↓ validation gates (gaps, stale data → halt downstream if red)
T1  Screener + Signal Engine (DCP)  → ~600 → ~40 candidates (pure code, ~zero cost)
     ↓
T2  Market Scanner Agent            → ~40 → ≤10 shortlist (one cheap agent pass)
     ↓
T3  Research Agents (parallel)      analyst + relevant sector specialist + macro overlay
     ↓                              per shortlisted name
T4  Quant Analysis attach (DCP)     signal scores, regime state, technical levels
     ↓
T5  Investment Committee (CIO)      memo per candidate: recommend / reject
     ↓ recommended only
T6  Risk Engine (DCP)               sizing, limits, stress marginal impact → PASS/FAIL
     ↓ CRO agent narrates; FAIL is terminal (no agent appeal path)
T7  Human Approval (dashboard)      approve / reject / expire (24h TTL); risk re-check at click
     ↓
T8  Execution (paper → live)        Trader agent note → DCP order params → broker adapter
     ↓
T9  Post-trade                      confirmation, reconciliation, event log, position update
```

Budget: per-day agent token budget with a hard circuit breaker (workflow halts, human notified). Target ≤10 committee runs/day; typical day: 0–3.

## 6. Technology stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | Typed (mypy strict on DCP modules) |
| API | FastAPI + Pydantic v2 | Pydantic schemas double as agent output validators |
| DB | PostgreSQL 16 | Single instance; logical separation by schema: `market`, `research`, `risk`, `trading`, `audit` |
| Cache/queue | Redis 7 | Signal cache, rate limiting, Redis Streams for the event bus |
| Orchestration | LangGraph-style state graph | Deterministic graph: nodes = agents/DCP steps, edges = typed states; every node run logged |
| Dashboard | Streamlit | Read via API; approval actions via authenticated API calls only |
| Infra | Docker Compose | Services: api, worker, scheduler, db, redis, dashboard |
| Testing | PyTest + hypothesis | Property tests on risk engine and sizing math |
| VCS | Git, trunk-based | CI gates in Doc 07 |

Event-driven communication: DCP and orchestrator publish domain events (`signal.generated`, `proposal.created`, `risk.check.completed`, `order.filled`) to Redis Streams; consumers are idempotent; every event is also persisted to `audit.decision_events` (Postgres is the source of truth, Redis is transport).

## 7. Security model

1. **Secrets**: broker + LLM keys in environment injection (Docker secrets), never in DB or code; Execution service is the sole holder of broker credentials.
2. **DB roles**: `agent_reader` (read-only on market/research), `dcp_writer`, `audit_writer` (INSERT-only on audit schema), `admin` (human, MFA-gated ops). Agents literally cannot write risk limits.
3. **Prompt injection defence**: external text (news, filings) is wrapped as untrusted data; agent outputs are schema-validated; no agent output is executed as code or query; the risk engine is unreachable from agent context.
4. **Approval auth**: dashboard sessions authenticated; approval endpoint requires re-auth token; live arming requires a second factor.
5. **Kill switches**: global halt flag checked by every workflow node; per-market halt; automatic halt on drawdown breakers (Doc 04) and cost breaker.

## 8. Audit and explainability model

Every material record is reconstructible:

```
"Why did we buy AVGO on 2026-08-14?"
 → proposal #P-2031
   → committee memo #M-871 (CIO, model claude-x@vN, prompt hash a1b2…)
     → research memo #R-455 (inputs: fundamentals snapshot S-99, news set N-12)
     → signals [SIG-8812 momentum v1.3.0 code@sha f00d…]
   → risk check #RC-3301 (limits version L-7, portfolio snapshot PS-140, PASS)
   → approval #A-77 (human, 2026-08-14T09:31 AEST, ip …)
   → order #O-556 → fill #F-902 (broker conf …)
```

Retention: indefinite for the audit schema. Reports: monthly attribution pack, quarterly compliance pack, on-demand decision reconstruction.

## 9. Failure modes and degradation

| Failure | Behaviour |
|---|---|
| Data feed stale/gapped | Downstream workflow blocked for affected market; existing stops still monitored via last-good data + alert |
| LLM API down | Reasoning plane pauses; DCP monitoring (stops, limits, reconciliation) unaffected — by design nothing safety-critical needs an LLM |
| Broker API down | Orders queue in `pending_submit` with TTL; human alerted |
| Agent emits invalid schema | Retry once, then mark run failed, exclude candidate, log event |
| Cost budget breached | Workflow halt + human notification |

The safety-critical path (risk limits, stop monitoring, reconciliation) has **zero LLM dependencies**. That is deliberate and non-negotiable.
