# 07 — Repository Structure

Atlas AI Capital · v1.0 · Monorepo · Python 3.12 · trunk-based, PR-gated

---

## 1. Layout

```
atlas/
├── pyproject.toml                 # uv/poetry; single lockfile
├── docker-compose.yml             # api, worker, scheduler, dashboard, db, redis
├── Makefile                       # make up / test / lint / backtest / migrate
├── .github/workflows/ci.yml
├── docs/                          # THIS package lives here, versioned with code
│   └── adr/                       # architecture decision records (append-only)
│
├── atlas/
│   ├── core/                      # shared kernel — zero business logic
│   │   ├── config.py              # pydantic-settings; env-injected secrets
│   │   ├── db.py  events.py       # session mgmt; transactional outbox
│   │   ├── audit.py               # event emitter + hash chain
│   │   └── clock.py               # injectable time — no naked datetime.now()
│   │
│   ├── dcp/                       # ★ DETERMINISTIC COMPUTE PLANE ★
│   │   ├── market_data/           # ingestion adapters, adjustment, quality gates
│   │   │   └── adapters/          # eodhd.py, nse_bhavcopy.py, …  (one interface)
│   │   ├── indicators/            # pure functions; no I/O; property-tested
│   │   ├── screener/
│   │   ├── signals/               # strategy implementations, versioned
│   │   │   ├── momentum/  quality_growth/  regime/
│   │   │   └── registry.py        # strategy version pinning (code_sha)
│   │   ├── backtest/              # event-driven engine, cost models, PIT joins
│   │   ├── risk/                  # ★ RiskEngine, sizing, limits, stress math ★
│   │   ├── portfolio/             # positions, P&L, attribution, FX translation
│   │   └── execution/             # order state machine + broker adapters
│   │       └── brokers/           # paper.py, ibkr.py  (one interface)
│   │
│   ├── agents/                    # ★ REASONING PLANE ★
│   │   ├── runtime/               # LLM client, budget guard, schema validation,
│   │   │                          # untrusted-content wrapping, run logging
│   │   ├── prompts/               # versioned templates — hashed at load
│   │   │   ├── constitution.md    # embedded in every agent
│   │   │   └── cio/  research/  macro/  sector/  quant/  validation/
│   │   │       cro/  stress/  pm/  attribution/  trader/  compliance/
│   │   ├── schemas/               # pydantic output models per agent (the boundary rule)
│   │   └── roles/                 # thin per-agent classes: assemble context → call → validate
│   │
│   ├── workflows/                 # LangGraph-style orchestration
│   │   ├── graph.py               # typed nodes/edges; versioned graph defs
│   │   ├── daily_cycle.py         # T0–T9 pipeline (Doc 01 §5)
│   │   ├── strategy_lifecycle.py  # draft→…→live transitions
│   │   └── monitoring.py          # stops, reconciliation, DD breakers  (LLM-free)
│   │
│   ├── api/                       # FastAPI app
│   │   ├── routers/               # mirrors Doc 06 resource map
│   │   ├── auth.py                # scopes, step-up
│   │   └── schemas/               # request/response models (shared with agents/schemas)
│   │
│   └── dashboard/                 # Streamlit; pure API client
│
├── migrations/                    # alembic
├── seeds/                         # instrument universe, scenario library, limit set v1
└── tests/
    ├── unit/                      #   mirrors atlas/ structure
    ├── property/                  #   hypothesis: sizing, limits, adjustment math
    ├── integration/               #   db + api + workflow (docker services)
    ├── backtest_regression/       #   golden backtests — results pinned; drift fails CI
    └── constitution/              #   ★ adversarial agent tests: injection attempts,
                                   #     schema-escape attempts, risk-bypass attempts
```

## 2. Module boundary rules (enforced by import-linter in CI)

1. `dcp/**` may not import `agents/**` — the compute plane never depends on LLMs.
2. `agents/**` may not import `dcp/risk` or `dcp/execution` — agents cannot reach the veto or the broker even in code.
3. Only `dcp/execution/brokers/*` may import broker SDKs; only `agents/runtime` may import the LLM SDK.
4. `core/` imports nothing from the rest of `atlas/`.
5. All timestamps via `core.clock` (backtests and replays need injectable time).

## 3. Testing strategy

| Layer | Approach | Gate |
|---|---|---|
| DCP math | unit + hypothesis property tests (e.g. sizing never exceeds any cap for any input) | 100% branch coverage on `dcp/risk` — non-negotiable |
| Backtester | golden regression backtests with pinned results | any drift fails CI |
| Risk engine | scenario table tests: every L-rule, every breaker transition | required for merge |
| Agents | schema validation tests + **constitution tests**: prompt-injection corpora, attempts to emit numbers into execution fields, attempts to argue past a FAIL | red-team suite runs nightly and on any prompt change |
| Workflow | integration: full T0–T9 on fixture data in Docker | pre-release |
| API | contract tests from OpenAPI; idempotency tests | required |

## 4. CI pipeline (GitHub Actions)

`lint (ruff) → type-check (mypy --strict on dcp/, core/) → import-linter → unit+property → migration check → integration (compose) → backtest-regression → constitution suite (nightly + prompt-diff triggered)`

Merge to main requires all gates green + one human review. Prompts and limit seeds diffs are flagged for mandatory extra review (CODEOWNERS).

## 5. Versioning and releases

Everything that affects decisions carries a version: strategy `code_sha`, prompt `template_hash`, model `version string`, limit set `version int`, workflow `graph version`. A release = a git tag pinning all five; the run logs record which release produced every decision. Rollback = redeploy prior tag; history is unaffected (append-only).

## 6. Local development

`make up` brings up compose with the paper broker and a seeded 2-year fixture dataset; `make daily-cycle DATE=2026-07-10` replays a full pipeline day deterministically. Deterministic replay on fixtures is the primary development loop — engineers should almost never need live data or live LLM calls (recorded agent outputs are replayable via the run log).
