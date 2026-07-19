# CLAUDE.md — Atlas AI Capital

AI hedge-fund operating system. Hypothetical A$100k, US + India equities, long-only
swing/position. **Capital preservation first.** Paper mode; live trading is Phase 7
and gated behind human arming. Nothing here is investment advice.

## Read first
- `docs/architecture/` — 9 design docs. 01 (system), 02 (Agent Constitution), 04 (risk), 05 (DB), 08 (roadmap) matter most.
- `docs/adr/` — 10 signed decisions. Load-bearing: 0006 (stop derivation), 0007 (universe), 0009 (approval bar = beat SPY total return, absolute), 0010 (first paper approval: xsmom, with demotion bands).
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
pytest             # currently 1354 passing (isolated to the atlas_test database)
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
- **P4 Risk Engine**: DONE — L1–L11 `validate()`, sizing §4, DD1–DD3 breakers, stress §7,
  factor overlap §12, approval re-check §2.2; `make cov-risk` enforces 100% branch coverage.
- **P5 Paper Trading**: DONE and merged to `main` (lifecycle schema 0010, PaperBroker
  next-session-open fills, build→approve-with-recheck→settle→snapshot, exits + FIFO
  sell settlement, T0–T9 daily cycle with in-process scheduler, reconciliation,
  console as sole control surface on port 8001).
- **First approved strategy (2026-07-13, ADR-0010)**: `xsmom-pit-tr` at state
  **'paper'** — 12-1 cross-sectional momentum, monthly; approved on regenerated
  artifacts (+737.31% vs SPY TR +593.89%, p=0.000, DSR 0.995, WF 4/4). Fully wired
  (migration 0020): quant.signals generation in the cycle (t6b), signal-first desk
  lane + SIGNALS evidence block, bridge resolves real signal UUIDs (fail-closed),
  daily band check (t5b) demotes to latched 'suspended' on DD −40% / 126-session
  excess −25pp. Caveats + tighten-only bands in the ADR.

## Task queue (priority order)
1. ~~**P1 exit — real data**~~ DONE (calendars XNYS/XASX, FX job, backfill CLI; 1y history per ADR-0004, zero red gates under per-instrument coverage rules).
2. ~~**Nightly chain verification**~~ DONE (`make verify-chain` / `atlas/tools/verify_chain.py`; tamper + deletion tested; schedule via cron and alert on non-zero exit).
3. ~~**First real backtest**~~ DONE — momentum v1 **failed the gates on real data** (both SPY and AVGO; see `docs/reports/first-real-backtest-momentum-v1.md`). Gates were not touched; verdicts recorded verbatim per the working-style rule below. Not decision-grade per ADR-0004 (1y window).
4. ~~**TradingAgents adoption**~~ DONE (ADR-0005): debate roles + CIO debate_summary, grounding verifier in run_agent, resumable workflow checkpoints (migrations 0007/0008), per-role model registry + OpenAICompatClient + shadow_mode. Deferred: sentiment analyst (needs social-media injection corpus).
5. ~~**P4 Risk Engine**~~ DONE (`atlas/dcp/risk/engine.py` + stress/factor_overlap/correlations/approval_recheck; 100% branch coverage via `make cov-risk`; property tests prove no input sizes past a cap).
6. ~~GitHub push + CI green~~ DONE (https://github.com/jkaryampudi/atlas).
7. ~~**P5 trading API + console**~~ DONE (`atlas/api/routers/trading.py`, console TRADING page).
8. ~~**P5 exits + daily pipeline**~~ DONE — GO-LIVE stack: exit engine (`atlas/dcp/trading/exits.py`: pre-authorized stop exits, discretionary close), sell settlement + FIFO lots, nightly incremental ingest (`atlas/dcp/market_data/daily.py` + `seeds/universe.json`), T0–T9 cycle (`atlas/ops/daily.py`, one atomic checkpointed transaction/day, settle-before-stops ordering), paper reconciliation (break = kill), alerts (`atlas/ops/alerts.py`, set `ATLAS_ALERT_URL`), launchd supervision + nightly `pg_dump` (`make install-ops`).
9. ~~**Memo→proposal bridge**~~ DONE (ADR-0006 stop derivation + ADR-0010 signal wiring; earnings calendar + regime + scanner context + SIGNALS in the evidence corpus; scorecard with dartboard baseline + source slices).
10. ~~**Post-ADR-0010 hardening bundle**~~ DONE — implementable variants (xsmom PASS; PEAD FAIL → ADR-0015 sleeve to 0), derived tighten-only bands + CUSUM, index-core 70/10/20 (ADR-0012/0014/0015), specialists + fundamentals in evidence, DD2/DD3 dual-confirm clearing (`risk/clearance.py`), attribution (0027), feature store p1 (0024), learning loop measured-only (0030), reliability layer (0031), **S&P 500 expansion + lineage-scoped DSR counting (ADR-0016 executed 2026-07-18: 511 US active, migration 0032, `activate_universe --reconcile` for semi-annual drift)**.
11. ~~**Research Factory phase 1** (= feature-store phase 2)~~ SHIPPED (91bcd72, 2026-07-18: `atlas/dcp/factory/` — frozen RecipeSpec v1 grammar, closed 4-member momentum catalog, gauntlet runner with registration-before-run + pre-committed kill leg, byte-identity equivalence pins vs production math; hardened by a 20-finding adversarial review). First real recipe runs 2026-07-20: specs in `docs/specs/`, reports in `docs/reports/recipe-*.md`, every trial counted against the momentum lineage.
12. **Next**: Factory **phase 2** — hypothesis engine + console surface for submitting/viewing recipe runs (runner is CLI-only; console is the sole control surface), then phase 3 adoption pipeline (survivors → impl-variant + draft ADR into the Principal's queue); opportunity-screen edge trial matures ~mid-Aug (monthly cohort automated in T9); learning-loop Tier-1 activation (Principal decision; needs ~60 sessions of matured labels); Linux-box migration (board #1 — deferred by Principal 2026-07-18); NIFTY 50 direct (blocked: EODHD has zero NSE coverage; vendor procurement is a Principal decision).

## Working style
- Tests first; golden pins for anything with numeric outputs.
- Honest failures are deliverables — the overfit canary test exists to prove gates reject junk. Never weaken a gate to make a strategy pass.
- New tables → new alembic migration (0005+), never edit applied migrations.
- Keep `README.md` phase checklists updated as items complete.
