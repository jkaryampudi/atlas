# 19 — File Index (document → source map)

> Maps each REVIEW_PACKAGE document to the primary source files a reviewer should open to
> verify or challenge it. Paths are repo-relative. This is a navigation aid, not exhaustive —
> each document carries its own inline citations.

## 01_EXECUTIVE_SUMMARY
Synthesis of all below. Key anchors: `docs/adr/0009,0010,0017`, `CLAUDE.md`, `README.md`,
`docs/reports/pit-fundamentals-vendor-decision.md`.

## 02_SYSTEM_ARCHITECTURE
- Wall enforcement: `tests/unit/test_boundaries.py`
- Planes: `atlas/dcp/**` (deterministic), `atlas/agents/**` (reasoning)
- Entry/services: `atlas/api/main.py`, `atlas/api/routers/*`, `atlas/ops/daily.py`, `atlas/ops/scheduler.py`
- Core: `atlas/core/{config,clock,db,audit_repo}.py`
- Docs: `docs/architecture/01-enterprise-architecture.md`, `07-repository-structure.md`

## 03_DATA_PIPELINE
- Vendor adapter: `atlas/dcp/market_data/adapters/eodhd.py`, `adapters/fixture.py`, `adapters/base.py`
- ETL: `atlas/dcp/market_data/{ingest,daily,backfill,adjustment,calendars,quality,fundamentals}.py`
- Manual batches (⚠ not on the nightly cadence): `atlas/dcp/market_data/dividends.py`, `quarterly_fundamentals.py`
- Tables: `market.*` (see migrations `0001`, `0012`, `0018`, `0024`, `0028`)
- Vendor gap: `docs/reports/pit-fundamentals-vendor-decision.md`

## 04_FACTOR_LIBRARY
- Signals: `atlas/dcp/signals/{momentum,trend,meanrev,breakout,pead,quality}/v1.py`, `xsmom/generate.py`
- Features: `atlas/dcp/features/{momentum,volatility,definitions,store}.py`
- Factory catalog: `atlas/dcp/factory/features.py`, `factory/families/{momentum,low_vol}.py`
- Graveyard evidence: `docs/reports/*.md`, `quant.trial_registry`

## 05_SCORING_ENGINE
- Ranking-to-capital (single signal): `atlas/dcp/signals/xsmom/generate.py`, `atlas/dcp/backtest/xsmom_pit_run.py`
- Sizing (NOT a score): `atlas/dcp/trading/bridge.py` (`SLEEVE_BUDGET_FRACTION`)
- Measured-never-applied ranking: `atlas/dcp/research/health_score.py`, `opportunity_screen.py`
- Conviction: `atlas/agents/roles/cio.py`, `atlas/agents/schemas/memo.py`
- Calibration (measured only): `atlas/dcp/learning/calibration.py`

## 06_PORTFOLIO_CONSTRUCTION
- Sizing/sleeve: `atlas/dcp/trading/bridge.py`; risk sizing §4: `atlas/dcp/risk/engine.py`
- Retired core: `atlas/dcp/trading/core_allocation.py`
- NAV/holdings: `atlas/dcp/portfolio/*`
- Decisions: `docs/adr/0012,0014,0015,0017`

## 07_RISK_MANAGEMENT
- Engine + limits: `atlas/dcp/risk/engine.py`, `seed_limits.py`, `risk.limit_sets` (v2)
- Controls: `atlas/dcp/risk/{stress,factor_overlap,correlations,vol_target,clearance,approval_recheck}.py`
- Stops: `atlas/dcp/trading/exits.py` (ADR-0006 derivation in `bridge.py`)
- Coverage: `Makefile` (`cov-risk`); Policy: `docs/architecture/04-risk-management-policy.md`

## 08_BACKTESTING
- Engine/portfolio: `atlas/dcp/backtest/{engine,portfolio,portfolio_validation,walkforward}.py`
- Gauntlet/approval: `atlas/dcp/backtest/{approval,registry,real_run}.py` (CostModel in `real_run.py`)
- Strategy runs: `xsmom_pit_run.py`, `impl_variant_run.py`, `pead_pit_run.py`, `quality_pit_run.py`
- No-look-ahead: `PanelView` in `portfolio.py`; PIT membership: `atlas/dcp/market_data/index_membership.py`

## 09_AI_AGENT_DESIGN
- Roles: `atlas/agents/roles/{cio,committee,debate,specialists}.py`; scanner: `atlas/dcp/scanner/v1.py`
- Runtime: `atlas/agents/runtime/{runner,grounding,registry,budget,llm}.py`
- Schemas (no-agent-numbers): `atlas/agents/schemas/*`
- Prompts (hashed): `atlas/agents/prompts/*`; desk driver: `atlas/agents/desk.py`, `atlas/ops/analyze.py`
- Shadow/learning: `atlas/agents/shadow_compare.py`, `atlas/dcp/learning/loop.py`
- Constitution: `atlas/agents/prompts/constitution.md`, `docs/architecture/02-agent-constitution.md`

## 10_CODEBASE_OVERVIEW
- Whole `atlas/` tree; hotspots: `atlas/dashboard/console.html` (~2,264 lines), `atlas/dcp/trading/bridge.py`,
  `atlas/dcp/backtest/portfolio_validation.py`, `atlas/dcp/risk/engine.py`
- Superseded/dead: `atlas/dashboard/overview.py` + `pages/*.py` (Streamlit, superseded by console.html)
- Declared-but-unused: Redis (`atlas/core/config.py` `redis_url`; no client in app code)

## 11_CONFIGURATION_REFERENCE
- `atlas/core/config.py`; env vars grepped across `atlas/` (`ATLAS_*`); budget caps in `atlas/agents/runtime/runner.py`
- Risk limits: `risk.limit_sets`; sleeve budgets: `atlas/dcp/trading/bridge.py`
- Ops env: `ops/run_api.sh`, `ops/run_daily.sh`, `ops/migrate_from_dump.sh`

## 12_DEPENDENCIES
- `pyproject.toml`; imports across `atlas/`; `atlas/agents/runtime/llm.py` (Anthropic HTTP);
  `atlas/dcp/market_data/adapters/eodhd.py` (EODHD); `docker-compose.yml`; `.github/workflows/ci.yml`

## 13_TESTING
- `tests/{unit,integration,constitution}/`; `tests/conftest.py` (isolation + self-heal)
- Config: `pyproject.toml` (`[tool.pytest]`, `[tool.mypy]`), `Makefile`; CI: `.github/workflows/ci.yml`

## 14_SECURITY
- `atlas/api/**` (no auth middleware, no CORS); `atlas/api/routers/trading.py` (deferred step-up)
- Secrets: `atlas/core/config.py`, `.env` (git-ignored), `.gitignore`
- Audit chain: `atlas/core/audit_repo.py`, `atlas/tools/verify_chain.py`

## 15_PERFORMANCE
- Cycle: `atlas/ops/daily.py`; screen timing: `atlas/dcp/research/opportunity_screen.py`
- DB indexes: migrations `migrations/versions/*`; test-suite timing: CI logs / local runs
- No formal benchmark harness exists (stated in the doc).

## 16 / 17 / 18 / 20 (this author's docs)
Synthesis across all of the above + `00_GROUND_TRUTH.md`, `docs/adr/*`, `docs/architecture/*`,
`docs/reports/*`. `18_REVIEW_CHECKLIST.md` cross-references specific files per question.

## Where the ground truth lives
- Signed decisions: `docs/adr/0001..0017`
- Architecture: `docs/architecture/01..08` + `README.md`
- Research verdicts: `docs/reports/*.md` (graveyard, first-real-backtest, vendor decision, shadow)
- Operating instructions / invariants: `CLAUDE.md`
- Schema evolution: `migrations/versions/0001..0032+`
