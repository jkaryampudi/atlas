# Atlas AI Capital — Phase 1 Foundation

AI investment operating system. Architecture package in `docs/architecture/` (approved per ADR-0001).

## Status: Phase 1 in progress — package v1.2 (ADR-0002 quant rigour, ADR-0003 learning loop)

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
pytest                    # 17 tests
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
- [ ] `/v1/market/*`, `/v1/portfolio/snapshot` read endpoints
- [ ] Streamlit Overview page (pure API client)
