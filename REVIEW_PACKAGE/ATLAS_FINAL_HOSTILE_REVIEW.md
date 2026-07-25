# Atlas — Final Hostile Review (P2 round 3)

Adversarial pass over the round-3 fixes; and an honest restatement of which
review attack scenarios remain exploitable.

## Fixes attacked (all held)
| Scenario (from the assignment §9) | Result |
|---|---|
| 12. Positive fold underperforms benchmark but passes | REFUSED — benchmark_folds gate (`test_approval_pg::test_f021_*`). Holds. |
| 17. Audit and trading locks acquired in reverse order | No ABBA — lifecycle path takes audit lock first (`test_lock_ordering_pg`). Holds. |
| 21. Stale approval attempts to execute an order | Voided fail-closed at settle (`test_stale_order_settle_guard_pg`). Holds. |
| 22. Failed kill evidence reused | REFUSED — mandatory-gate check (`test_approval_pg::test_f024_*`). Holds. |
| 7. Cash-dividend factor treated as a split | Quarantined by `is_valid_split_ratio` (`test_split_factor_validation`). Holds. |
| 6. Dividend revision changes total returns | Dividends now refreshed nightly + idempotent (`test_dividend_nightly_refresh_pg`). Holds (refresh); see F-009 for basis. |
| 25. Previously fixed F-003/F-004/F-008/F-013/F-016 regress | Full suite green; none regressed. |
| 24. PostgreSQL unavailable during full verification | `ATLAS_REQUIRE_PG=1` run exits 4, not green (M31). Holds. |

## Scenarios that REMAIN exploitable (open findings — honest)
- **1. Provider revises a historical close** (F-007) — still overwrites in place; past runs unreconstructable.
- **2/3. Reused ticker joins old issuer / same ticker two venues** (F-002) — no issuer identity; only the zero-era case is excluded (F-001 partial).
- **8. Split-adjusted estimate mixed with unadjusted actual** (F-009) — EPS split-basis not versioned.
- **9. ADR ratio / FX false valuation signal** (F-010) — cross-currency ratios not normalised.
- **10. AUD portfolio vs USD benchmark** (F-006) — live attribution alpha still currency-mismatched.
- **11. DSR trial dispersion omitted** (F-005) — corrected estimator variance, but the expected-max dispersion is still the inflating fallback by default (not threaded through the gate).
- **13/14/15/16. Audit actor/entity change; final events removed; anchor rollback** (F-019, F-020) — hash omits entity/actor; tail truncation still undetected.
- **18/19/20. Scheduler dies / misses / double-claims a cycle** (F-025) — no durable supervision yet.
- **23. Monthly rebalance retains an ineligible position** (F-012) — no rebalance-sell.

## New defects introduced by the round-3 changes
None of Critical/High severity. The full suite (1675 passed) and the invariant
tests (two-plane wall, clock, audit tamper) confirm no regression.

## Verdict
The round-3 fixes are correct and regression-protected, but **the completion gate
is not met** — the audit-backbone, issuer-identity, versioned-data, currency, and
strategy-revalidation findings remain open and their attack scenarios remain live.
