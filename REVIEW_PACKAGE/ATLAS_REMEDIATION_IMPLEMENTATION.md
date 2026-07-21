# Atlas — Remediation Implementation (P2 Critical/High)

**Branch:** `p2-critical-high-remediation` (from production baseline `54c55a8`) · **Date:** 2026-07-21

> **Honest completion status — the gate is NOT met.** This assignment asked for `unresolved Critical + High = 0` across 1 Critical + 26 High findings. That set includes genuinely multi-day architectural changes (a full issuer-identity subsystem, versioned bitemporal ingestion, a complete API authentication layer). Rather than rush unverifiable changes into a financial system's validation, ingestion, and security substrate — which the assignment explicitly forbids ("do not misclassify or downgrade to make the gate pass") — this increment delivers **5 High findings fully remediated with regression proof, 1 Critical partially remediated (the unambiguous contamination closed and verified), and the test-integrity meta-gap closed**, and documents the remainder honestly as scoped follow-up increments. See `ATLAS_REMEDIATION_EVIDENCE.md` for per-finding proof and `ATLAS_POST_REMEDIATION_SELF_REVIEW.md` for the hostile review.

## Findings addressed this increment

| Finding | Sev | Status | Commit |
|---|---|---|---|
| F-003 entry-day return double-count | High | **FIXED** (hand-calc goldens; fails against old engine) | `P2.2` |
| F-004 impossible/optimistic stop fills | High | **FIXED** (gap-through fills at open; fail-closed on invalid open) | `P2.2` |
| F-011 dead §12 momentum overlay (`MUTANT_no_such_state`) | High | **FIXED** (restored `('paper','live')`; overlay-fires PG test) | `P2.3` |
| F-017 `make replay` contaminates prod DB | High | **FIXED** (disposable-DB guard + Makefile) | `P2.3` |
| F-008 future-dated earnings actuals | High | **FIXED** (parse + store PIT guards; the PVH case) | `P2.3b` |
| F-001 wrong-era/wrong-issuer PIT splice | **Critical** | **PARTIAL** (unambiguous zero-era contamination excluded, verified vs ADT/VAL/MNK; full fix needs F-002) | `P2.4` |
| M31 silently-skipped PG tests | Med (meta) | **FIXED** (`ATLAS_REQUIRE_PG` hard-fail; CI wired; PG-less run exits non-zero) | `P2.8` |

## Behaviour & architecture changes

- **Backtest engine** (`atlas/dcp/backtest/engine.py`): the exit-day mark now uses the same `>=` prior-day-close rule as the hold path (no entry-day double-count); stop exits fill at `costs.sell(min(stop, open))` and fail closed on a non-finite/non-positive open. No API change. Pre-existing goldens in `test_backtest_engine`, `test_signals_{trend,breakout,meanrev}` were **re-pinned** to the corrected values (old→new recorded in each docstring); the trend/breakout stop goldens had literally hard-coded sells at prices the bar never traded.
- **Risk overlay** (`atlas/dcp/trading/proposals.py`): the §12 momentum attribution filter changed from the unmatchable `MUTANT_no_such_state` sentinel to `('paper','live')`. The overlay now actually binds; behaviour is unchanged in production today because the only momentum family (`xsmom-pit-tr`) is `research_shadow`, but a promotion would now correctly cap momentum-factor exposure.
- **Earnings ingestion** (`atlas/dcp/market_data/earnings_history.py`): `parse_earnings_history` gained an optional `known_as_of` receipt anchor and excludes `report_date <= period_end` and `report_date > known_as_of`; `store_surprises` re-checks as belt-and-suspenders. **Guards NEW ingestion only** — quarantining the 67 pre-existing future-dated rows and adding a DB CHECK is a follow-up (it mutates stored data and warrants its own reviewed migration).
- **PIT panel** (`atlas/dcp/backtest/xsmom_pit_run.py` + `index_membership.py`): new `series_overlaps_membership()` fail-closed guard excludes any member whose stored series has zero bars inside its membership era (the reused-ticker cases). Wired before the completeness check. **Validated magnitudes unchanged** (these series were never rankable via `is_member_on`); the guard makes the exclusion explicit at the source.
- **Replay** (`atlas/dcp/market_data/replay.py` + `Makefile`): `assert_disposable_db()` refuses a non-`*_test`/non-`replay` target unless `--force`; `make replay` targets `ATLAS_REPLAY_DATABASE_URL` (default `atlas_test`).
- **Test integrity** (`tests/conftest.py` + `.github/workflows/ci.yml`): `pytest_configure` errors when `ATLAS_REQUIRE_PG=1` and Postgres is unreachable; CI sets the flag.

## Schema / migrations

**None.** Every fix in this increment is behavioural + test-level; migration head remains `0035` and an empty-DB→head apply is clean. The deferred findings below (F-002 issuer identity, F-007 versioned ingestion, an earnings-CHECK, audit-hash epoch) each require a new migration and are called out.

## Compatibility

No public API or schema change. Re-pinned goldens are the only test-surface changes and each records its old→new. The `ATLAS_REQUIRE_PG` guard is opt-in (unset = legacy behaviour), so existing unit-only workflows are unaffected.

## Operational actions still required (cannot be done in-repo)

- **F-013 (deferred):** the live EODHD API key has leaked into the audit chain + logs historically. **The operator must rotate the EODHD key out-of-band** and move it to a request header (code change) — see deferred list. Claude Code cannot rotate an external credential.

## Deliberately deferred findings (NOT started — honest accounting)

Each needs its own reviewed increment; none is safe to rush:

- **F-002** issuer identity (FIGI/ISIN + symbol-change) — schema + data; the prerequisite for *completing* F-001 and for held-position wrong-issuer resolution.
- **F-005** Deflated Sharpe variance (`1/T` → empirical PSR/DSR) — tractable math + numerical fixtures; **not done**.
- **F-006** AUD/USD benchmark currency consistency — **not done**.
- **F-007** versioned/bitemporal bar ingestion (revision detection + as-of reads) — schema + reader rework; **not done**.
- **F-012** deployed monthly rebalance-sell vs validated construct — **not done**.
- **F-013** secret redaction (token→header, scrub, rotation) — **not done** (operator rotation required regardless).
- **F-016** API authentication/authorisation on state-mutating endpoints — **not done**.
- **F-019** audit hash coverage of entity/actor columns — **not done** (needs a chain epoch).
- **F-020** audit tail-truncation detection (external anchor) — **not done**.
- **F-021** walk-forward gate benchmark-relative folds — **not done**.
- **F-022** ADR-0018 re-promotion legacy conditional hole — **not done**.
- **F-023** lineage-tag catalog binding — **not done**.
- **F-024** `pead-sue-tr` authoritative on failed-kill evidence — **not done**.
- **F-025** scheduler dead-man / durable failure records — **not done**.
- **F-026** stale pre-downgrade approved orders cancellation — **not done** (automated capital path already blocked by the P0.1 guard).

---

## P2 round 2 addendum

Additional findings closed after the initial increment (commits `P2.5`, `P2.6a-c`):
- **F-013** (secret redaction) and **F-016** (API auth) — `atlas/core/secrets.py`, `atlas/api/auth.py`; auth on the 5 mutating trading endpoints (fail-closed, `ATLAS_API_TOKEN` only). Operator must rotate the EODHD key out-of-band.
- **F-023** (lineage catalog) — `registry.KNOWN_LINEAGES`; unknown lineage refused.
- **F-022** (unconditional promotion identity) — `require_signed_validation_artifact` now verifies a matching stamped identity for every promotion.
- **F-005** (Deflated Sharpe) — **PARTIAL**: PSR skew/kurtosis denominator + empirical-dispersion capability + numerical tests; runner/gate threading pending.

**Still deferred (deep/backbone/schema — not started):** F-002 (issuer identity), F-006 (currency), F-007 (versioned ingestion), F-012 (rebalance), F-019/F-020 (audit hash epoch + tail anchor — backbone migrations), F-021 (WF benchmark-relative — approval gate), F-024 (pead label/kill-gate; F-022 now blocks the promotion vector), F-025 (scheduler dead-man), F-026 (stale-order cleanup — automated path already guarded).

**Running total:** 9 High + M31 fully remediated; F-001 (Critical) + F-005 partial. Completion gate still NOT met.
