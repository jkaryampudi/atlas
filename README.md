# Atlas AI Capital

AI investment operating system — hypothetical A$100k, US + India equities, long-only,
**paper mode**, capital preservation first. Architecture in `docs/architecture/`;
signed decisions in `docs/adr/` (18 ADRs). Nothing here is investment advice.

## Status (2026-07-20): the momentum sleeve is in RESEARCH_SHADOW pending re-validation

> **ADR-0018 (2026-07-20):** an independent review returned REJECT STRATEGY
> EVIDENCE for the executable `xsmom-pit-tr` (deployed price-return signal ≠
> validated total-return signal; DSR ≈0.85 at the lineage count; not
> reproducible). The strategy is **downgraded from `paper` to `research_shadow`**:
> non-authoritative, deploys no capital, never reported as validated, and
> un-promotable without a fresh signed validation artifact. The 40% sleeve below
> is now **frozen as shadow exposure** — the fund's honest state is **zero
> validated deployed strategies** pending re-validation. No strategy math,
> parameters, or historical numbers changed.

- **Book (ADR-0017, signed 2026-07-20)**: SATELLITE-HEAVY, **no ETFs** by
  Principal directive — momentum sleeve `xsmom-pit-tr` at **40% of NAV**
  (top-5 individual stocks, 8%/name = the L1 cap; L3 sector clustering shaves
  honestly), remainder cash (≥10% L5); the former 70% SPY/INDA core is
  RETIRED (`core_allocation.CORE_RETIRED`, T8c reports it nightly); PEAD
  sleeve stays 0 (ADR-0015); India deferred until an NSE vendor decision.
  Costs signed in ink: ≈−16pp crash exposure before the −40% band demotes
  (no fallback sleeve), structural cash drag vs SPY printed by attribution.
- **Research Factory (phases 1+2)**: console-driven recipe gauntlet — author
  a pre-registered hypothesis on the QUANT page (bounded grammar, rationale
  required, lineage-bound, kill-start pre-committed), run the full five-gate
  gauntlet, browse every verdict. Registration chokepoint enforces one-name-
  one-experiment under a cross-process advisory lock (`--rerun` is the
  explicit counted repeat). Catalog: momentum family (4, fully mined —
  first-light verdicts 2026-07-20: 6-1 STRIKE by its own kill leg, 3-1 and
  12-0 FAIL) + low-vol family (`low_vol_252`, byte-identity anchored to the
  production risk panel — first burn, Principal-authored from the console,
  FAIL both legs: DSR 0.986 but null p=0.79; a monkey trade on raw returns).
  Audited `repin_features` tool for reviewed spec-identical refactors
  (stored values must reproduce byte-for-byte before a pin moves).
- **Research surfaces**: per-name dossier (financials, Atlas's own valuation
  + health score + fragility markers, committee cross-check) at
  `/console/dossier`; whole-universe Opportunity Screen (deterministic,
  measured-never-applied) with monthly cohorts auto-recorded into the
  source-pick edge trial; investing.com picks graded nightly vs SPY + a
  dartboard (first 20-session verdicts ~2026-08-07); sonnet-5 shadow
  comparison complete (debate diversity 5/8 vs 1/8 — switch is a Principal
  registry decision).

Phases 1–5 as originally built (first strategy approved to PAPER 2026-07-13):

- **P1 data plane**: EODHD All-In-One, deep backfill 2010→2026, 693 instruments,
  exchange calendars, FX, dividends (total-return capable), earnings calendar,
  quality gates, nightly incremental ingest; estimate-revisions forward archive
  (`market.estimate_snapshots`, migration 0028 — append-only daily PIT snapshots
  of the vendor-overwritten Earnings::Trend consensus, the ADR-0011 forward-
  paper-trial prerequisite; research-usable after ~6 months of accrual).
- **P2 agents**: 5 roles + bull/bear debate (per-side model routing, all four
  cases persisted), grounding cage (token-boundary, fail-closed), budget
  sub-caps, red-team suite; committee memos graded by a scorecard (vs SPY at
  20/60 sessions, dartboard baseline, dissent grading, per-source slices).
- **P3 quant**: trial registry (51 trials across 9 lineages, deflated Sharpe at the true LINEAGE count per ADR-0016),
  1000-path null gate, purged walk-forward, point-in-time S&P 500 membership,
  total-return scoring. Graveyard on record: momentum v1, trend/meanrev/breakout,
  FX sandbox (ADR-0008) — all failed verbatim; gates never touched.
- **Research Factory v1 (feature-store phase 2, 2026-07-18)**: spec-driven
  recipe gauntlet on the point-in-time feature store (`atlas/dcp/factory/`) —
  frozen bounded RecipeSpec grammar (canonical spec_hash; no rationale → no
  run; lineage + kill-start pre-committed), bounded momentum feature family
  (momentum_12_1/6_1/3_1/12_0, production math, hashed code), trials
  registered and committed durably BEFORE the gauntlet runs (count honesty,
  dataset_version + hypothesis provenance), full gauntlet reused by import;
  golden-pinned byte-identical to the impl-variant xsmom path in the
  ranking-coincident regime (on real data the recipe deliberately ranks on
  the live generator's price basis — a different, honestly-counted trial).
  `python -m atlas.dcp.factory.recipe_run --spec … [--dry-run]`.
- **P4 risk**: L1–L11, sizing, DD1–DD3 latched breakers, stress, factor overlap,
  approval re-check; 100% branch coverage enforced (`make cov-risk`).
- **P5 paper trading**: proposals→approval-with-recheck→next-open fills→FIFO
  settlement→stops/exits→reconciliation; T0–T9 daily cycle (checkpointed,
  atomic per day) on an in-process scheduler; console at `/console` (port 8001)
  is the sole control surface; nightly backups via the in-process
  scheduler (the launchd variants are dead on macOS — TCC blocks
  `~/Documents` for background agents; first successful backup
  2026-07-21).
- **Attribution (ADR-0012 cons. 4 + Doc 04 §14)**: daily core(beta)/satellite
  (alpha)/cash NAV decomposition (`reporting.attribution_daily`, migration
  0027; flow-adjusted returns, SPY-TR / signed 55:15 blend benchmarks), t8b
  cycle node, monthly report CLI (`python -m atlas.dcp.reporting.attribution`)
  with the §14 shortfall standing line, `/v1/portfolio/attribution/daily` +
  console card. Hand-derived golden pins; contributions sum to the NAV change
  exactly.
- **ADR-0010 (2026-07-13)**: `xsmom-pit-tr` (12-1 cross-sectional momentum,
  monthly, winner decile) approved for **paper** on regenerated artifacts —
  +737.31% vs SPY TR +593.89%, p=0.000, DSR 0.995, WF 4/4 — with caveats
  accepted in ink and tighten-only demotion bands (DD −40%, 126-session excess
  −25pp → latched `suspended`). Signals, desk lane, real signal lineage on
  proposals, and the daily band check are live (migration 0020).

Historical build log below (Phase-1-era detail kept for the record).

ADR-0005 additions (TradingAgents patterns under Atlas governance):
- **Bull/bear debate** (`agents/roles/debate.py`): 1 case + 1 rebuttal per side, forced concessions, stance integrity, exec-number guards; CIO memo gains `debate_summary`. Advisory only — red-team proves a unanimous debate cannot open a BUY without DCP evidence
- **Grounded-number verification** (`agents/runtime/grounding.py`): every numeric token in narrative output must appear verbatim in cited evidence (whitelist: L/DD rule IDs, years); fail-closed with `agent.grounding.failed` audit events
- **Resumable workflow checkpoints** (`core/workflow.py`, migration 0007): nodes persist results before the next runs; same run_id resumes without re-executing completed nodes; replay daily cycle wired
- **Per-role model registry** (`agents/runtime/registry.py`): `ATLAS_MODEL_<ROLE>` → `ATLAS_MODEL_DEFAULT` resolution; `local/` prefix routes to `OpenAICompatClient` at `ATLAS_LOCAL_LLM_URL`; `shadow_mode` runs logged non-actionable (Constitution 7.2, migration 0008)

**First real backtest (task 3): momentum v1 FAILED the gates on real data** — see `docs/reports/first-real-backtest-momentum-v1.md` (verbatim verdicts, ADR-0004 small-sample warning). Gates unmodified; failure recorded as a valid result.

v1.2 additions built and tested:
- **Agent calibration** (`dcp/learning/calibration.py`): Brier-scored conviction weights with shrinkage, clipped [0.5, 1.5]
- **CUSUM drift detector** (`dcp/learning/drift.py`): live-vs-backtest degradation, latched breach
- **Volatility targeting** (`dcp/risk/vol_target.py`): property-tested invariants — never exceeds 0.80 gross, bounded daily step, breaker states dominate
- **Migration 0002**: `learning` schema (outcome_labels, counterfactuals, agent_calibration, lessons, adjustments) + `quant.trial_registry`
- Constitution **Article 10** (learning tiers), Risk Policy §11–14, roadmap gate amendments

Built and tested in this drop:
- **Core kernel**: env-based config, injectable clock (`FrozenClock` for deterministic replay), append-only **audit hash chain** with tamper detection (`atlas/core/audit.py`)
- **Market-data plane**: vendor adapter interface (EODHD chosen, fixture adapter default), split adjustment with continuity guarantees, **data-quality gates** (missing day → RED, unexplained >40% move → AMBER)
- **Portfolio math**: FX-translated NAV in AUD, long-only enforcement, L11 non-AUD exposure input — verified to the cent against hand calculation + property-tested with hypothesis
- **Schema migration 0001**: `market`, `risk`, `trading`, `audit` schemas; `risk.limit_sets` with the dual-confirmation ≥1h CHECK; audit role scaffolding (INSERT-only; agents get **no** grants on `risk.*`)
- **API**: `/v1/system/health`, `/v1/system/mode` (paper, never armed in Phase 1)
- **Seeds**: limit set v1 (`small_aum` per ADR-0001), starter instrument universe (SPY/QQQ/AVGO/MSFT + India via INDA/NDIA/ADRs)

## Quick start
```bash
pip install -e ".[dev]"
pytest                    # 1515 tests (PG tests isolated to atlas_test, auto-created; bootstrap self-heals)
docker compose up -d      # postgres + redis + api
alembic upgrade head
```

## Remaining for Phase 1 exit (see docs/architecture/08-development-roadmap.md)
- [x] EODHD live adapter + ingestion job writing `price_bars_daily` and gates (vendor symbol map; exchange calendars XNYS/XASX replace weekend-skip)
- [x] FX rates ingestion (`fx_rates_daily`): daily job `python -m atlas.dcp.market_data.fx --date …` + range backfill
- [x] History backfill for seed universe; zero red gates on a clean day — **1 year accepted per ADR-0004** (EODHD tier caps history at 1y): 2025-07-11→2026-07-10 backfilled, 2,262 bars + 313 FX rows, 505/505 gates green. 2-year re-run pending plan upgrade.
- [x] Golden ingestion regression tests (fixture backfill week incl. split-explained move; honest-RED market test)
- [x] Audit repository (Postgres-backed chain) + nightly verification job (`make verify-chain`, exits non-zero on any tamper/deletion; cron: `0 3 * * * cd <repo> && make verify-chain || <alert>`)
- [x] `make replay DATE=…` end-to-end on fixtures (gate=green, chain verified)
- [x] `/v1/market/*`, `/v1/portfolio/snapshot` read endpoints (full read surface: market, portfolio, risk, research, quant, trading, audit, system)
- [x] ~~Streamlit Overview page~~ superseded by the single-file console at `/console` (pure API client; decision flow, approval queue, live-cycle animation, scorecard, analyze box, strategy card)
