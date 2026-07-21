# Atlas — Final Remediation Evidence (P2, all rounds)

Branch `p2-critical-high-remediation` from baseline `54c55a8`. This is the
consolidated, honest status of every Critical/High finding after three
remediation rounds. **The completion gate (unresolved Critical/High = 0) is NOT
met**: 15 High are fully fixed with regression proof, the Critical (F-001) and
one High (F-005) are partial, and 9 High remain open — the latter dominated by
genuinely multi-day schema / audit-backbone / strategy-revalidation builds that
were deliberately not rushed into capital-adjacent infrastructure.

## Complete finding table

| ID | Sev | Status | Proof / reason |
|----|-----|--------|----------------|
| F-001 | Critical | **PARTIAL** | zero-era / reused-ticker exclusion verified vs live ADT/VAL/MNK (`test_membership_era_guard`); full fix needs F-002 issuer identity |
| F-002 | High | **OPEN** | bitemporal issuer/instrument/listing identity — schema + data + resolution layer; a multi-day subsystem |
| F-003 | High | **FIXED** | entry-day counted once; hand-calc goldens fail vs old engine |
| F-004 | High | **FIXED** | stops fill at `min(stop, open)`; 7 gap cases |
| F-005 | High | **PARTIAL** | PSR skew/kurtosis denominator + empirical-dispersion capability + numerical tests; threading dispersion through 8 runners/gate + re-pin is the finish step |
| F-006 | High | **OPEN** | currency-consistent alpha in live attribution (AUD book vs USD SPY) — reporting-basis change + golden re-pin |
| F-007 | High | **OPEN** | versioned/bitemporal bar ingestion + as-of reads — schema + reader rework |
| F-008 | High | **FIXED** | future-dated earnings excluded at parse+store (`test_earnings_history_pit_guard`) |
| F-009 | High | **OPEN** | split-basis consistency in earnings surprises — versioning of the EPS basis |
| F-010 | High | **OPEN** | ADR cross-currency ratio normalisation — field-level currency model |
| F-011 | High | **FIXED** | §12 momentum overlay restored (`test_momentum_overlay_fires_pg`) |
| F-012 | High | **OPEN** | monthly rebalance-sell — strategy behaviour change requiring full re-validation |
| F-013 | High | **FIXED** | secret redaction, canary-tested; + operator key rotation |
| F-014 | High | **FIXED** | split-factor validation; invalid ratios quarantined (`test_split_factor_validation`) |
| F-015 | High | **FIXED** | nightly dividend refresh (`test_dividend_nightly_refresh_pg`) |
| F-016 | High | **FIXED** | fail-closed API auth on mutating endpoints (`test_api_auth`) |
| F-017 | High | **FIXED** | replay disposable-DB guard (`test_replay_guard`) |
| F-018 | High | **FIXED** | global lock order; ABBA blocked (`test_lock_ordering_pg`) |
| F-019 | High | **OPEN** | audit hash to cover entity/actor — chain epoch (backbone) |
| F-020 | High | **OPEN** | audit tail-truncation anchor — backbone migration; the probed blind spot remains |
| F-021 | High | **FIXED** | walk-forward gate is benchmark-relative (`test_approval_pg` F-021 cases) |
| F-022 | High | **FIXED** | unconditional promotion-identity match |
| F-023 | High | **FIXED** | lineage catalog; unknown refused |
| F-024 | High | **FIXED (app)** | failed mandatory gate is terminal for approval; existing pead row demotion = operator action |
| F-025 | High | **OPEN** | durable scheduler supervision + dead-man — needs a cycle-record table (migration) |
| F-026 | High | **FIXED (app)** | settle refuses a stale non-authoritative buy (`test_stale_order_settle_guard_pg`); cancelling the 2 existing orders = operator action |
| M31 | Med | **FIXED** | PG-less run fails (`exit 4`), not false-green |

**Tally:** 15 High FIXED + F-024/F-026 application-fixed (operator action for the
residual data) + M31 · F-001 (Critical) + F-005 PARTIAL · 9 High OPEN.

## Round-3 additions (this cycle) — per-finding

- **F-021** — `benchmark_folds` added to all three walk-forward variants (net-of-cost
  fold return beating the benchmark over the same window/currency basis); runners
  pass SPY buy-and-hold; `evaluate_approval` requires a majority to beat the
  benchmark and fails closed with no benchmark. A 4/4-positive-but-1/4-beat run is
  now REFUSED (the old positive-folds check passed it).
- **F-018** — `atlas/core/locks.py` canonical order (audit → lifecycle);
  `acquire_trading_lifecycle_lock` takes the audit lock first. Two-connection test
  proves the lifecycle path blocks on the audit lock (no ABBA).
- **F-024** — `evaluate_approval(mandatory_gates=...)` refuses on any failed gate;
  `approve_pead_paper` passes the failed 2016 kill → can no longer promote.
- **F-026** — `settle_orders` resolves each buy's signal lineage; a non-authoritative
  (post-downgrade) buy is voided fail-closed, never filled. Control: an
  authoritative buy still fills.
- **F-014** — `is_valid_split_ratio`; `record_split` quarantines non-split factors.
- **F-015** — `_ingest_market` fetches + records dividends nightly, idempotent.

## Deferred — why not FIXED this session (honest)

- **F-002 / F-007** — a bitemporal issuer-identity subsystem and versioned
  bitemporal ingestion are each multi-day schema+data builds; done wrong they
  corrupt the definitive research panel or every historical read. F-001's full
  closure depends on F-002.
- **F-019 / F-020** — changing the audit hash formula and adding a protected tail
  anchor is a migration to the integrity backbone that interacts with dozens of
  tests that truncate the chain; probed and scoped, not rushed.
- **F-012** — a strategy-behaviour change that the assignment itself requires be
  fully re-validated (before/after backtest, turnover/cost/drawdown/capacity);
  the re-validation is a large empirical exercise, not a code edit.
- **F-005 finish / F-006 / F-009 / F-010 / F-025** — moderate; each is a
  self-contained next increment (dispersion threading + re-pin; reporting-currency
  basis + re-pin; EPS split-basis versioning; ADR field-currency model; a
  scheduler cycle-record table + supervision).

## Gates (final, hermetic rebuild)
See the final report for exact numbers: pytest all-pass (0 failed), ruff clean,
mypy clean (134 files), verify-chain green, cov-risk 100%, empty-DB→head clean,
PG-less run exits 4.

## Previous fixes preserved
All round-1/round-2 fixes (F-003, F-004, F-008, F-011, F-013, F-016, F-017, F-022,
F-023, M31) still pass; the new schema/lock/settle changes do not bypass auth,
secret redaction, trade approval, replay isolation, kill gates, or the PG-skip
guard (verified by full-suite green).
