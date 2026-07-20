# Atlas AI Capital — Ground-Truth Facts (for the REVIEW_PACKAGE authors)

Anchor EVERY claim to this file or to code you read. If a fact here conflicts with the
code, TRUST THE CODE and flag the discrepancy. Prefer evidence from code over intent.
This is a **paper-mode research/simulation system**, months old, built rapidly by one
Principal + an AI pair. Do NOT oversell it. The reviewer (GPT-5.6) is hostile; every
unsupported claim will be caught and reflects badly.

## What Atlas IS (one paragraph)
An AI hedge-fund *operating system* running a **hypothetical A$100k, US + India equities,
long-only, paper mode ONLY**. Live trading is an unbuilt future phase gated behind human
"arming". It combines a deterministic quant/execution plane (`atlas/dcp`) with an
LLM agent "research desk" (`atlas/agents`) separated by a hard architectural wall, an
append-only audit hash-chain, a risk engine, a point-in-time backtest gauntlet, and a
single-file web console as the sole control surface. **Nothing here is investment advice;
no real capital; no broker connection (a PaperBroker simulates next-open fills).**

## Size (verified `wc -l`, 2026-07-20)
- `atlas/` production Python ≈ **37,000 LOC**: dcp 26,308 (119 files) · agents 3,755 (25) ·
  api 2,473 (14) · ops 2,065 (8) · tools 1,675 (10) · core 347 (7) · dashboard 196.
- Console: single HTML file `atlas/dashboard/console.html` ≈ **2,264 lines** (inline JS/CSS).
- Tests: **36,501 LOC across 215 files**, ~1,515 tests passing (pytest collection incl. parametrized; ~1,454 `def test_` functions) (unit 754 / integration
  625 / constitution 75). Full suite passes; ~65s wall.
- 34 alembic migrations (0001..0032 + a few) · 17 signed ADRs · 9 architecture docs · 22 reports.
- DB (Postgres) schemas: market, quant, trading, research, audit, learning, reporting, risk, fxlab.

## dcp subpackage LOC map
backtest 7,390 · market_data 4,777 · trading 3,435 · research 2,159 · signals 1,570 ·
factory 1,513 · reporting 1,146 · risk 1,123 · features 959 · learning 853 · scanner 334 ·
portfolio 239 · execution 172 · indicators 115.

## HARD INVARIANTS (enforced by tests; the design's spine)
1. **Two-plane wall**: `atlas/dcp/**` never imports `atlas/agents/**`; agents never import
   `atlas.dcp.risk`/`execution` (`tests/unit/test_boundaries.py`).
2. **No agent numbers**: LLM output never becomes a sizing/pricing/execution value —
   Pydantic schemas reject it; a BUY without DCP evidence refs is a validation error.
3. **Risk FAIL is terminal**: no flag bypasses a risk check.
4. **Audit append-only**: `audit.decision_events` is a hash chain; never UPDATE/DELETE.
5. **Prompts are code**: templates in `atlas/agents/prompts/` hashed + pinned per run.
6. **Injectable time**: timestamps via `atlas.core.clock`, never `datetime.now()` (ops layer
   is the documented exception for WHEN-to-run decisions).
7. **Every backtest registers a trial** (`quant.trial_registry`); deflated Sharpe uses the
   true (lineage-scoped, ADR-0016) count; null-model + purged walk-forward required for approval.
8. **No look-ahead**: strategies receive only `bars[:i+1]`; structural, not by convention.

## Current portfolio / strategy state (the honest core)
- **ONE validated strategy exists**: `xsmom-pit-tr` — 12-1 cross-sectional momentum, monthly,
  top-5, state 'paper' (ADR-0010). Approved on regenerated artifacts: **+737% vs SPY TR +594%,
  null p=0.000, deflated Sharpe 0.995, walk-forward 4/4**. CAVEAT: this is a *concentrated top-5
  momentum backtest* — high headline return travels with large drawdown (its own demotion band
  is −40%); the DSR/null/WF are the real evidence, not the +737%.
- **ADR-0017 (signed 2026-07-20)**: book is now **satellite-heavy, NO ETFs** — momentum sleeve
  40% of NAV (8%/name = the L1 cap), remainder cash; the former 70% SPY/INDA index core is
  RETIRED. Consequence: **the entire invested book rests on ONE strategy** (concentration).
- **PEAD (`pead-sue-tr`)**: approved to paper (ADR-0013) then SUSPENDED to 0% budget (ADR-0015)
  because its *implementable* top-5 form failed the null model (p=0.132). Runs as a forward
  experiment; deploys no capital.
- **Graveyard (verdicts in ink, gates never weakened)**: momentum-v1 (decision-grade FAIL on
  real data), trend/meanrev/breakout (12/12 FAIL), quality-GP/A (FAIL p=0.387), low-vol
  (`low_vol_252`, FAIL both legs 2026-07-20: DSR 0.986 but null p=0.79), FX sandbox (3/3 FAIL).
  Momentum grid: 3-1 FAIL, 12-0 FAIL, 6-1 STRIKE (main PASS, pre-committed kill leg FAIL).
- **Trial registry**: 51 trials across 9 lineages (momentum 23, pead 7, breakout/trend/meanrev 4
  each, fxlab 3, quality 2, low-vol 2, momentum+pead 2).
- **THE BOOK IS CURRENTLY 100% CASH** (found 2026-07-20): core proposals always expired
  unapproved; the Principal approved AMD+INTC on 2026-07-18 (fill at the next cycle).

## Data plane (single vendor — lock-in)
- **EODHD "All-In-One" ($99/mo)** is the ONLY market-data vendor: daily bars (~2.47M rows),
  FX, dividends (total-return capable), splits/corporate actions, **current** fundamentals
  snapshots (526 names), earnings calendar, earnings surprises, estimate snapshots (2,386,
  a forward PIT archive accruing since ~2026-07). Fixture adapter for keyless dev.
- **CRITICAL DATA GAP**: EODHD provides NO point-in-time fundamentals (restated in place, no
  filing dates) and NO fundamentals for pre-2018 delistings. This BLOCKS honest value/quality
  factor families. A vendor decision (Sharadar SF1 recommended, ~$69/mo add-on) is OPEN,
  awaiting the Principal — see `docs/reports/pit-fundamentals-vendor-decision.md`. Until then,
  value/quality are IMPOSSIBLE to build honestly and are NOT built.
- History depth accepted at ~1yr for some gates (ADR-0004, EODHD tier); deeper 2012→ backfill
  exists for the S&P 500 PIT panel used by the momentum gauntlet. Universe: full S&P 500
  (~506 active US + 196 inactive/delisted carried for PIT membership), India via ADRs only
  (direct NSE is BLOCKED — EODHD has zero NSE coverage).

## Reasoning plane — the AI desk (`atlas/agents`)
- Roles: scanner, five specialist analysts (quality/growth/macro + bull/bear debate), CIO
  (`committee_memo`). Prompts in `atlas/agents/prompts/` (constitution prepended, hashed).
- Models: Anthropic via a per-role registry (`ATLAS_MODEL_<ROLE>` → `ATLAS_MODEL_DEFAULT`);
  OpenAI-compatible local option. Budget breaker $10/day global, sub-caps nightly $6 / analyze
  $5 (raised from $3 by the Principal today) / shadow $3.
- **Grounding cage**: every numeric token in narrative must appear verbatim in cited evidence,
  else the run fails closed (`agent.grounding.failed`). SCHEMA_MAX_ATTEMPTS=3 retries then hold.
- Agents produce MEMOS ONLY (recommendation + thesis + kill criteria + dissent + evidence refs).
  A deterministic **bridge** (`atlas/dcp/trading/bridge.py`, ADR-0006) turns a signed-strategy
  BUY memo into a sized, stop-derived, risk-checked proposal. No LLM number ever sizes anything.
- Shadow-mode model comparison (sonnet-5 vs sonnet-4-6) done 2026-07-20 (report exists); a
  model switch is a Principal registry decision. Learning loop is MEASURED, NEVER APPLIED
  (Brier-scored conviction weights computed + displayed, applied nowhere; Tier-1 activation is
  a future Principal signature, ~60 sessions of labels needed).

## Risk engine (`atlas/dcp/risk`)
- Limit set v2 (`small_aum`): L1 max stock 8% · L2 max ETF 15% / core-ETF 60% · L3 sector 25% ·
  L4 India sleeve 30% · L5 min cash 10% · L6 risk/trade 1% · L7 aggregate open risk 6% · L8
  correlation threshold 0.8 / combined-corr weight 12% · L9 max 2 new positions/day · L10 max
  5% ADV · L11 max non-AUD 85%. Values live in `risk.limit_sets` (versioned, dual-confirm CHECK).
- DD1–DD3 drawdown breakers (latched), stress §7, factor-overlap §12, correlations, approval
  re-check §2.2, vol-target (property-tested: never > 0.80 gross). `make cov-risk` enforces
  **100% branch coverage on the risk engine** (the only module with that bar).
- NOT IMPLEMENTED: VaR, CVaR, explicit beta targeting, portfolio optimizer (equal-weight only),
  intraday risk, live market-risk monitoring. Stops are pre-authorized exits, scanned daily
  (T4), derived by ADR-0006 (2×ATR or −10% floor). No live stop monitoring (daily granularity).

## Backtest gauntlet (`atlas/dcp/backtest`, 7,390 LOC)
- PIT S&P 500 membership (fail-closed interval rule, delisted included, SPY outside universe),
  total-return convention (ADR-0009 binding benchmark). Delisting-aware portfolio engine,
  monthly rebalance, next-open execution, committed CostModel **10 bps/side (5 commission + 5
  slippage) — a FLAT assumption, no spread/impact/borrow modelling**.
- Gates: 1000-path seeded monkey null (p ≤ 0.05), deflated Sharpe ≥ 0.9 at true lineage count,
  beat SPY buy-and-hold total return (absolute, ADR-0009), purged+embargoed walk-forward k=4.
- NOT IMPLEMENTED as gates: Monte Carlo path simulation beyond the monkey-null, formal
  cross-validation beyond walk-forward, regime/scenario stress on strategies, capacity analysis.

## Research Factory (`atlas/dcp/factory`) — phase 1+2
- Console-driven recipe gauntlet: bounded RecipeSpec grammar, closed feature catalog (momentum
  family fully mined + low_vol_252), registration-before-run + advisory-locked chokepoint (one
  name one experiment; `--rerun` explicit), pre-committed demote-only kill leg, audited
  `repin_features` tool. Feature store (`quant.feature_values`, PIT, dataset_version pins).
  Machine-AUTHORED hypotheses (phase 3) NOT built; adoption pipeline NOT built.

## Research surfaces (measured-never-applied, `atlas/dcp/research`)
- Per-name dossier (financials, Atlas's own valuation models + health score + fragility markers,
  committee cross-check); whole-universe Opportunity Screen; investing.com source-pick edge
  trial (graded vs SPY + dartboard). ALL are RESEARCH AIDS that reach no capital (invariant 2).
  Valuation models are educational, assumption-sensitive, explicitly NOT price targets.

## Operations / deployment — THE WEAK SPOT
- **Single machine**: the Principal's MacBook. Postgres in Docker. API on port 8001.
- The API process IS the scheduler (`ATLAS_INPROC_SCHEDULER=1`): T0–T9 daily cycle at 23:30 UTC
  + pg_dump backup at 00:30 UTC, in one atomic checkpointed transaction/day.
- **launchd supervision is DEAD**: macOS TCC blocks launchd from `~/Documents` (exit 127 since
  install) — so the "redundant" scheduler + backup jobs NEVER RAN. **The fund has taken ZERO
  backups to date** (first ever is scheduled tonight, 2026-07-21, via the in-process scheduler).
- iCloud syncs `~/Documents` → conflict-copy files under heavy writes. A Linux-box migration is
  RECOMMENDED and DEFERRED by the Principal. Provisioning + dump-restore scripts exist (`ops/`).
- No CI/CD pipeline that runs the suite on push (GitHub repo exists, "CI green" claimed in an
  ADR but verify). No containerized app deploy; no orchestration; no HA; no monitoring/alerting
  beyond an optional ntfy webhook (`ATLAS_ALERT_URL`, often unset → degrades to stderr).

## Security posture — THE OTHER WEAK SPOT (state plainly)
- **The API has NO request authentication or authorization.** Single principal on a localhost
  console; `trading.py` says step-up-token/scope plumbing is "deferred to the auth phase";
  `acknowledged_risks` IS enforced but there is no login, no session, no RBAC, no CORS config.
- Secrets in a plaintext `.env` (Anthropic + EODHD keys, DB creds) loaded by pydantic-settings;
  the DB password is a literal default `atlas_local_only`. No secrets manager, no encryption at
  rest, no TLS on the API. Repo is PUBLIC on GitHub (`.env` gitignored). Key rotation is a
  standing TODO. Human approval ("arming") for live is designed but live mode is UNBUILT.
- The Anthropic key incident today: a manual restart dropped it → 401s; nothing loads `.env`
  into os.environ automatically (the agent runtime reads it from the process env).

## Testing posture
- 1,515 tests, strong on determinism/golden-pins/property tests (hypothesis) and a 75-test
  "constitution" red-team suite proving the cage holds. mypy strict on core+dcp+fxlab only
  (api/ops/agents not strict-gated). 100% branch coverage ONLY on the risk engine (`make
  cov-risk`); global coverage is NOT measured/enforced. No performance/load/stress test tier.
  Tests isolated to `atlas_test` DB; a self-healing bootstrap (learned today: migration-cycle
  tests burn Postgres's 1600-column budget → the test DB is rebuilt on upgrade failure).

## Known technical-debt / honesty flags to SURFACE (non-exhaustive)
- console.html is a 2,264-line single file (inline JS) — a maintainability hotspot.
- Concentration risk: one strategy, one sleeve, one machine, one data vendor, one Principal.
- Flat 10bps cost model; no slippage/impact/spread/borrow; monthly-granularity stops.
- No PIT fundamentals → value/quality unbuildable now (the single biggest research blocker).
- Learning loop, opportunity-screen edge, shadow comparison, PEAD forward experiment, estimate-
  revisions archive: all EXPERIMENTAL / accruing / not-yet-actionable — label them as such.
- Paper-only: no real fills, no real slippage, PaperBroker fills at next session open at the
  open price (optimistic vs real markets). Reconciliation is paper-vs-paper.
- "First approved strategy" headline +737% must always carry its drawdown + concentration caveat.
- Suite ran green but several bugs were found+fixed THIS WEEK (flaky-suite ghosts, SPY resolution
  tiebreak, ATLZ test-DB leak, retry-desync) — the codebase is actively churning.
