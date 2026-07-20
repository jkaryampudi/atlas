# Unauthenticated mutation endpoints — inventory (P0, ADR-0018 objective 6)

**As of 2026-07-20, branch `p0-research-shadow`.** This inventory *identifies* the
API's unauthenticated state-mutating endpoints; it does **not** add authentication
(that is a later phase, deliberately out of P0 scope). The docker-compose API is
now bound to `127.0.0.1` (loopback only, ADR-0018), so these are reachable only
from the local host — but no code enforces identity or authorization.

## Posture (verified)

- **Zero authentication/authorization anywhere in `atlas/api/`.** A repo-wide grep
  for `Depends` / `Security` / `HTTPBearer` / `APIKeyHeader` / `OAuth2` / `api_key`
  / `Authorization` returns no matches. The app is a bare `FastAPI(...)`
  (`atlas/api/main.py:52`) with no global `dependencies=` and no middleware; every
  router is a bare `APIRouter()`. The no-auth state is documented in-code
  (`trading.py:14-17`: step-up/scope plumbing "deferred to the auth phase").
- **16 state-mutating endpoints, all `POST`** (zero `PUT`/`PATCH`/`DELETE`). The
  other 6 routers (portfolio, learning, market, audit, reporting, quant) are
  read-only.

## The 16 unauthenticated mutations

| # | Method + path | Handler (file:line) | Mutates |
|---|---|---|---|
| 1 | POST /v1/system/run-daily | `system.py:70` | fires the whole T0–T9 cycle (settle, stops, ingest, snapshot) |
| 2 | POST /v1/research/analyze | `research.py:103` | queues a desk analysis (LLM spend; writes a memo) |
| 3 | POST /v1/research/opportunities/run | `research.py:147` | whole-universe opportunity screen |
| 4 | POST /v1/research/opportunities/track | `research.py:177` | writes top-K into `research.source_picks` |
| 5 | POST /v1/research/source-picks/ingest | `research.py:215` | writes source_picks + PIT features (+ optional LLM memos) |
| 6 | POST /v1/research/source-picks/grade | `research.py:262` | write-once grades in `research.source_picks` |
| 7 | POST /v1/research/memos/{id}/review | `research.py:588` | upserts `memo_reviews` + appends an audit event as `actor_id='principal'` (unauthenticated impersonation of the human) |
| 8 | POST /v1/risk/breaker-clearances | `risk.py:133` | creates a drawdown-breaker clearance request (Confirmation A) |
| 9 | POST /v1/risk/breaker-clearances/{id}/confirm | `risk.py:146` | **lowers a latched drawdown breaker → resumes trading** (Confirmation B); only a 1h gap, not identity, separates A and B |
| 10 | POST /v1/trading/proposals/{id}/approve | `trading.py:144` | **the approval desk**: re-runs risk, on PASS commits an order toward capital |
| 11 | POST /v1/trading/proposals/{id}/reject | `trading.py:171` | transitions a proposal to rejected |
| 12 | POST /v1/trading/orders/{id}/cancel | `trading.py:189` | cancels an order |
| 13 | POST /v1/trading/positions/{id}/close | `trading.py:227` | creates a discretionary exit proposal |
| 14 | POST /v1/trading/settle | `trading.py:264` | **fills every pending order** via PaperBroker; creates executions, opens/closes positions (no body, no args) |
| 15 | POST /v1/factory/recipes/run | `factory.py:79` | runs a gauntlet that **registers counted trials** in `quant.trial_registry` (burns lineage trials / affects DSR counts) |
| 16 | POST /v1/risk/preflight | `risk.py:195` | a POST that is DELIBERATELY zero-write (dry-run; ends in `rollback()`) — listed for completeness; **not** a mutation |

**Safety-critical subset for a later auth phase:** #9 (resumes trading after a
drawdown halt), #10 + #14 (advance proposals to orders and fill them), #7
(impersonates the Principal on the audit chain), #15 (mutates the trial registry
that gates strategy approval).

## Where a future auth guard would attach (do NOT implement in P0)

Three attach levels, all currently empty:
1. **Global** — `FastAPI(dependencies=[Depends(require_principal)])` at
   `main.py:52` (covers all routes, including reads).
2. **Per-router** — `dependencies=` on the mutating routers (trading, risk,
   research, system, factory) at `app.include_router(...)`, leaving read routers
   open.
3. **Per-endpoint** — a `Depends(require_principal)` on each `@router.post`; the
   only way to auth mutations while leaving co-located GETs open (trading.py mixes
   both in one router).

Until then, the controls are: the loopback bind (ADR-0018), the Tailscale
boundary (`docs/ops/deploy-local.md`), and the deterministic risk wall (two-plane
boundary, no-agent-numbers, the fresh approval re-check) — none of which is a
substitute for authentication.
