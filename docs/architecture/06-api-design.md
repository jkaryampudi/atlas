# 06 — API Design

Atlas AI Capital · v1.0 · FastAPI · Base path `/v1` · JSON everywhere · Pydantic v2 schemas shared with agent output validation

---

## 1. Design rules

1. The API is the **only** write path to the platform — the Streamlit dashboard, CLI tools, and scheduler all go through it. No direct DB writes from clients.
2. State transitions, not field edits: clients call transition endpoints (`/approve`, `/halt`) rather than PATCHing state columns; the server enforces the state machine.
3. Idempotency keys required on all mutating endpoints (`Idempotency-Key` header).
4. Every mutating call emits an `audit.decision_events` row within the same transaction.
5. AuthN: session tokens for the dashboard; scoped service tokens for internal services. AuthZ scopes: `read`, `approve_trades`, `manage_limits`, `arm_live`, `admin`. Sensitive scopes (`approve_trades`, `manage_limits`, `arm_live`) require step-up re-authentication per action.

## 2. Resource map

```
/v1
├── /market
│   ├── GET  /instruments                 ?market=US&active=true
│   ├── GET  /instruments/{id}/bars       ?from&to
│   ├── GET  /quality-gates               data-quality status per market
│   └── POST /ingest/runs                 (service) trigger/record ingestion
├── /research
│   ├── GET  /memos                       ?type=committee&instrument=…
│   ├── GET  /memos/{id}                  full memo + evidence refs
│   ├── GET  /agent-runs                  ?agent_role&status&date
│   └── GET  /watchlist
├── /quant
│   ├── GET  /strategies                  lifecycle states, versions
│   ├── POST /strategies/{id}/transitions body {to: 'validated'|'approved'|…}  (guarded)
│   ├── GET  /signals                     ?date&strategy&instrument
│   ├── GET  /backtests/{id}              metrics + artifact refs
│   └── GET  /validation-reports/{id}
├── /risk
│   ├── GET  /limit-sets/current
│   ├── POST /limit-sets                  human-only; starts dual-confirmation flow
│   ├── POST /limit-sets/{id}/confirm     second confirmation (≥1h later, enforced)
│   ├── GET  /checks/{id}
│   ├── GET  /drawdown                    current DD state, breaker level
│   ├── GET  /stress-runs                 ?scenario&date
│   └── POST /halts   /halts/{id}/end     end requires dual confirmation
├── /portfolio
│   ├── GET  /snapshot                    current NAV, holdings, exposures, open risk
│   ├── GET  /positions  /positions/{id}
│   ├── GET  /performance                 ?period → TWR, Sharpe, vs benchmark, cost drag
│   └── GET  /attribution/{period}
├── /trading
│   ├── GET  /proposals                   ?state=pending_approval
│   ├── GET  /proposals/{id}              full evidence bundle (memo+signals+risk check)
│   ├── POST /proposals/{id}/approve      scope approve_trades + step-up; re-runs risk check
│   ├── POST /proposals/{id}/reject       body {reason}
│   ├── GET  /orders  /orders/{id}
│   └── GET  /reconciliations             ?date
├── /system
│   ├── GET  /health                      feeds, broker, LLM, queue lag, cost budget
│   ├── GET  /mode                        {trading_mode: 'paper'|'live', armed: bool}
│   ├── POST /mode/arm-live               scope arm_live + step-up; expires end-of-day
│   └── GET  /costs                       daily LLM/broker cost vs budget
└── /audit
    ├── GET  /events                      ?entity_id&type&from&to (read-only, paginated)
    ├── GET  /events/verify               hash-chain verification status
    └── GET  /decisions/{proposal_id}/reconstruct   full lineage tree (Doc 01 §8)
```

## 3. Key contracts

### 3.1 Trade proposal (GET /trading/proposals/{id})

```json
{
  "id": "0197f…", "state": "pending_approval", "expires_at": "2026-08-15T09:30:00+10:00",
  "symbol": "AVGO", "market": "US", "action": "buy",
  "investment_thesis": "…summary…",
  "committee_memo_id": "…", "signal_ids": ["…"],
  "fundamental_analysis_ref": "memo:R-455",
  "technical_analysis": {"signal": "momentum/v1.3.0", "features_ref": "sig:8812"},
  "quant_score": 0.74, "risk_score": 0.31,
  "entry_price": 172.40, "stop_loss": 158.90, "target_price": 205.00,
  "position_size": 8, "position_value_aud": 2130.50,
  "confidence": "MEDIUM",
  "risks": [{"risk": "customer concentration", "evidence_ref": "memo:R-455#s4"}],
  "risk_check": {"id": "RC-3301", "verdict": "PASS", "results": [
      {"rule": "L1", "value": 0.021, "limit": 0.08, "pass": true},
      {"rule": "L6", "value": 0.0099, "limit": 0.01, "pass": true}]},
  "kill_criteria": [{"condition": "revenue growth < 5% YoY", "review": "quarterly"}]
}
```

This is the required trade-proposal format from the brief, extended with lineage references so the dashboard can render the full evidence bundle on one screen.

### 3.2 Approval (POST /trading/proposals/{id}/approve)

Request: `{"step_up_token": "…", "acknowledged_risks": true}`
Server sequence (single transaction): verify scope + step-up → verify not expired → **re-run risk check** on fresh snapshot → if FAIL respond `409 RISK_RECHECK_FAILED` with itemised results → else record approval, create order in `pending_submit`, emit events.

### 3.3 Errors

Uniform envelope `{error: {code, message, details}}`. Notable codes: `RISK_RECHECK_FAILED`, `PROPOSAL_EXPIRED`, `HALT_ACTIVE`, `DATA_GATE_RED`, `DUAL_CONFIRM_TOO_SOON`, `NOT_ARMED` (live order attempted without daily arming), `BUDGET_EXHAUSTED`.

## 4. Event model (Redis Streams → consumers)

Streams: `events.market`, `events.workflow`, `events.trading`, `events.risk`.
Event envelope: `{event_id, type, entity, payload, occurred_at, producer, schema_version}`.
Consumers are idempotent (dedupe on `event_id`); Postgres `audit.decision_events` is written first (transactional outbox pattern), then relayed to Redis — the audit log can never lag the bus.

Core event types: `market.bars.ingested`, `market.gate.changed`, `signal.generated`, `scanner.shortlist.created`, `memo.published`, `proposal.created`, `risk.check.completed`, `proposal.approved|rejected|expired`, `order.state_changed`, `execution.recorded`, `reconciliation.break`, `drawdown.breaker.changed`, `halt.triggered|ended`, `agent.run.completed`, `cost.budget.breached`.

## 5. Internal orchestrator interface

The LangGraph-style workflow runs in the worker service and talks to the API with a service token. Each graph node = one API-visible unit of work; node results persist before the next node runs (crash-resumable). Graph definition is versioned; the workflow run record pins the graph version — so "what process produced this decision" is answerable historically.

## 6. Dashboard (Streamlit) contract

Pages: Overview (NAV, DD state, breaker level, gates, mode/armed), Approval Queue (evidence bundle per proposal, approve/reject with step-up), Portfolio (positions, theses, kill criteria, stops), Research (memos browser), Risk (limit utilisation, stress results, halts), Performance (attribution, cost drag incl. LLM spend), Audit (decision reconstruction viewer).

The dashboard holds no credentials beyond the user session and performs no computation — it is a pure API client. This keeps the approval surface auditable and lets us swap Streamlit later (Challenge A7) without touching business logic.
