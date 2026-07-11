# CLAUDE.md — Atlas AI Capital

AI hedge-fund operating system. Hypothetical A$100k, US + India equities, long-only
swing/position. **Capital preservation first.** Paper mode; live trading is Phase 7
and gated behind human arming. Nothing here is investment advice.

## Read first
- `docs/architecture/` — 9 design docs. 01 (system), 02 (Agent Constitution), 04 (risk), 05 (DB), 08 (roadmap) matter most.
- `docs/adr/` — 3 signed decisions (small_aum limits, India via ETFs/ADRs, EODHD vendor, learning tiers).
- `README.md` — phase-by-phase status checklists.

## Hard invariants — tests enforce these; NEVER violate
1. **Two-plane wall**: `atlas/dcp/**` never imports `atlas/agents/**`; agents never import `atlas.dcp.risk` or `atlas.dcp.execution` (`tests/unit/test_boundaries.py`).
2. **No agent numbers**: LLM agents never produce values used for sizing/pricing/execution — enforced by Pydantic schemas in `atlas/agents/schemas/`. A BUY without DCP evidence refs is a validation error.
3. **Risk FAIL is terminal**: no code path may let an agent or convenience flag bypass a risk check.
4. **Audit is append-only**: `audit.decision_events` is a hash chain; never UPDATE/DELETE it; every material action emits an event.
5. **Prompts are code**: templates in `atlas/agents/prompts/` are hashed and pinned per run; changing one is a reviewed change.
6. **Injectable time**: all timestamps via `atlas.core.clock`, never `datetime.now()`.
7. **Every backtest registers a trial** (`quant.trial_registry`); deflated Sharpe must use the true count. The null-model gate + purged walk-forward are required for strategy approval (`dcp/backtest/approval.py`).
8. **No look-ahead**: strategies receive only `bars[:i+1]`; keep it structural.

## Quality gates — all must pass before any commit
```bash
make doctor        # environment diagnosis
pytest             # currently 125 passing (isolated to the atlas_test database)
ruff check atlas tests
mypy               # strict on atlas/core + atlas/dcp
```
Local stack: `docker compose up -d db redis`, then
`export ATLAS_DATABASE_URL="postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas"`,
`alembic upgrade head`. API on **port 8001** on this machine (8000 is taken by
another project). Deterministic replay: `make replay DATE=2024-07-15` → gate=green.

## Status (as of handoff)
- **P0 Architecture**: signed. **P1 Foundation**: near-exit — see remaining below.
- **P2 Agents**: runtime + 5 roles + 9-test red-team suite done; live-model evals pending (needs Anthropic API key).
- **P3 Quant**: engine, momentum v1, null-model gate, walk-forward, regime classifier, artifact approval — all done on synthetic fixtures; overfit canary criterion PASSED. Real-data runs pending backfill.
- **P4 Risk Engine**: not started (next major build — design in docs 04).

## Task queue (priority order)
1. ~~**P1 exit — real data**~~ DONE (calendars XNYS/XASX, FX job, backfill CLI; 1y history per ADR-0004, zero red gates under per-instrument coverage rules).
2. ~~**Nightly chain verification**~~ DONE (`make verify-chain` / `atlas/tools/verify_chain.py`; tamper + deletion tested; schedule via cron and alert on non-zero exit).
3. ~~**First real backtest**~~ DONE — momentum v1 **failed the gates on real data** (both SPY and AVGO; see `docs/reports/first-real-backtest-momentum-v1.md`). Gates were not touched; verdicts recorded verbatim per the working-style rule below. Not decision-grade per ADR-0004 (1y window).
4. ~~**TradingAgents adoption**~~ DONE (ADR-0005): debate roles + CIO debate_summary, grounding verifier in run_agent, resumable workflow checkpoints (migrations 0007/0008), per-role model registry + OpenAICompatClient + shadow_mode. Deferred: sentiment analyst (needs social-media injection corpus).
5. **P4 Risk Engine** (`atlas/dcp/risk/engine.py`): implement L1–L11 from `docs/architecture/04` §3 against `risk.limit_sets` v1 (already seeded via `dcp/risk/seed_limits.py`), sizing per §4, drawdown breakers per §5, pro-forma portfolio math. Requirement: 100% branch coverage on `dcp/risk`, property tests proving no input produces a size violating any cap. `vol_target.py` already exists — wire breaker-state dominance.
6. GitHub push + CI green (workflow ships in `.github/workflows/ci.yml`).

## Working style
- Tests first; golden pins for anything with numeric outputs.
- Honest failures are deliverables — the overfit canary test exists to prove gates reject junk. Never weaken a gate to make a strategy pass.
- New tables → new alembic migration (0005+), never edit applied migrations.
- Keep `README.md` phase checklists updated as items complete.
