# Atlas — Requirements Traceability

*Maps the load-bearing methodology / architecture / governance requirements to their implementation, test coverage, evidence, and status. Reviewed at `d65e0b1`.*

**Status legend:** ✅ MATCHES · 🟡 PARTIALLY MATCHES · ❌ DOES NOT MATCH · ⬜ NOT IMPLEMENTED · ❓ UNABLE TO VERIFY

| # | Requirement (source) | Implementation | Test | Evidence | Status |
|---|----------------------|----------------|------|----------|--------|
| R1 | Two-plane wall: `dcp` never imports `agents`; agents never import `dcp.risk`/`dcp.execution` (CLAUDE.md inv-1) | AST import wall | `tests/unit/test_boundaries.py` | Passes; **but blind to relative/dynamic imports** (M34) | 🟡 |
| R2 | No agent numbers: LLM never produces sizing/pricing values (inv-2) | Pydantic schemas in `atlas/agents/schemas/`; bridge derives numbers | schema tests | BUY without DCP evidence refs rejected | ✅ |
| R3 | Risk FAIL is terminal, no bypass (inv-3, Constitution 3.2) | `risk/engine.py validate()` no short-circuit; no override flag | `make cov-risk` 100% | Verified: no bypass path; every rule always evaluated | ✅ |
| R4 | Audit append-only hash chain; every material action emits an event (inv-4) | `audit.py`/`audit_repo.py`; INSERT-only grant | tamper + interior-delete tests | Chain detects payload tamper & interior delete; **tail-truncatable (F-020), physically mutable by owner (P3/P7), hash omits entity/actor (F-019), 16/18 voids emit no event (M54)** | 🟡 |
| R5 | Prompts are code: hashed & pinned per run (inv-5) | `runner.py:200-204` sha256(constitution+template) | recorded in `research.agent_runs.prompt_template_hash` | Prompts hashed; **seeds/universe manifests not** (S17) | 🟡 |
| R6 | Injectable time, never `datetime.now()` (inv-6) | `atlas/core/clock.py`; threaded through most paths | `tests/unit/test_clock.py` | **~30 wall-clock calls in `atlas/`; no enforcing test; violated at `ingest_picks.py:97`** (M48) | ❌ |
| R7 | Every backtest registers a trial; DSR uses the true count (inv-7) | factory register-before-run; `registry.py`; lineage-scoped | `test_trial_lineage_pg.py` | Holds in factory; **non-factory runners register-after-run; engine unguarded; lineage self-declared (F-023)** | 🟡 |
| R8 | No look-ahead: strategies get only `bars[:i+1]`, structural (inv-8) | backtest slicing; PEAD effective_index | backtest/PEAD tests | Structural logic correct; **data substrate is not PIT (F-001/F-007/F-008)** | 🟡 |
| R9 | Approval bar = beat SPY **total** return, absolute (ADR-0009) | `portfolio_validation.py`; benchmark = SPY TR | validation tests | **Scorecard/attribution grade price-return excess in places; AUD vs USD (F-006); WF gate not benchmark-relative (F-021)** | 🟡 |
| R10 | Deflated Sharpe ≥ 0.90, p ≤ 0.05 gates | `validation.py`; `approval.py` | gate tests, overfit canary | Gates wired & the canary rejects junk; **DSR uses `V[SR]=1/T` — overstated (F-005); honest DSR ≈0.85 grandfathered (R-02)** | 🟡 |
| R11 | Purged + embargoed walk-forward, majority folds | `walkforward.py`; `approval.py:71` | WF tests | Runs; **counts absolute-positive folds, not excess-over-benchmark (F-021)** | 🟡 |
| R12 | `xsmom-pit-tr` = `research_shadow`, deploys no capital, fail-closed re-promotion (ADR-0018) | `downgrade_xsmom_shadow.py`; bridge/settle guard; `strategies.state` | `test_research_shadow_*` | State correct; automated capital blocked (verified); **re-promotion gate has a legacy conditional hole (F-022); stale approved orders remain (F-026)** | 🟡 |
| R13 | Momentum sleeve ≤ 40% of NAV at the L1 cap edge; remainder cash; PEAD 0 (ADR-0017/0015) | `bridge.py SLEEVE_BUDGET_FRACTION=0.40`; pead sleeve 0.00 | sleeve tests | Fractions correct; **`pead-sue-tr` authoritative `paper` on failed-kill evidence (F-024); no monthly rebalance-sell (F-012)** | 🟡 |
| R14 | ETF core RETIRED — no ETFs (ADR-0017) | `core_allocation.py CORE_RETIRED`; t8c "core retired" | core tests | Retired in code; **limit_set v2 still grants SPY/INDA 0.60 L2 cap (M39)** | 🟡 |
| R15 | Stop derivation ADR-0006 (entry/ATR-stop/2R-target) | `bridge.py` | bridge tests | Deterministic, DCP-derived | ✅ |
| R16 | Universe = S&P 500 expansion, lineage-scoped DSR (ADR-0016, 511 US active) | `activate_universe`; `validation.index_membership` | universe tests | 511 actives; **lineage tags self-declared outside factory (F-023); membership ~68% early-window, one spell/ticker (M1/M2)** | 🟡 |
| R17 | Point-in-time datasets / no survivorship in validation | `xsmom_pit_run.py`; index_membership | PIT run tests | Backtest keeps delisted names (good); **wrong-era/wrong-issuer contamination (F-001), no issuer identity (F-002)** | ❌ |
| R18 | Corporate actions correct (splits/dividends) | `adjustment.py`, `total_return.py` | CA tests | Split direction + TR reinvestment correct (spot-checked); **earnings split-basis drift (F-009); non-split factors applied (F-014); dividends not refreshed (F-015)** | 🟡 |
| R19 | Transaction costs modeled (ADR-0017 "costs in ink") | `CostModel` 5bps commission + 5bps slippage | cost tests | Consistent backtest/paper; **FX conversion cost unmodeled; India/US micro-charges absent (structural for India, S20)** | 🟡 |
| R20 | Reproducible / deterministic replay (`make replay` → gate=green) | `replay.py`; `WorkflowRunner` | replay integration | Deterministic within one env; **`make replay` contaminates prod DB (F-017); no lockfile, py3.14 vs 3.12 (M49); in-place overwrites (F-007)** | ❌ |
| R21 | Data provider = EODHD, US+India via US listings; zero NSE | `eodhd.py`; calendars US/AU only | ingest tests | Confirmed; India via ADRs only (S20) | ✅ |
| R22 | Paper trading DONE (lifecycle, next-open fills, reconciliation=kill) — README P5 | `proposals.py`, `paper.py`, `exits.py`, `ops/daily.py` | lifecycle PG tests | Lifecycle robust; **no API auth (F-016); missed cycles (F-025); no durable failure record (M56)** | 🟡 |
| R23 | Live trading gated behind human arming (Phase 7) | `system` router `armed:False` hardcoded | — | Live trading structurally absent | ✅ |
| R24 | Nightly chain verification scheduled | cycle t1 + `make verify-chain` | verify-chain run | Runs in-cycle (169 events) + CLI (1,889 verified); **launchd cron dead (TCC); tail-truncation blind (F-020)** | 🟡 |
| R25 | Feature source content-pinned (`code_sha` refuses divergence) | `features/store.py:127-170` | feature pin tests | Strong — editing feature math without re-approval is refused | ✅ |

**Summary:** of 25 load-bearing requirements — ✅ 8 fully met · 🟡 14 partial · ❌ 3 not met (R6 injectable-time, R17 PIT/survivorship identity, R20 reproducibility) · ⬜/❓ 0. The three ❌ are the spine of the "NOT YET TRUSTWORTHY" verdict.
