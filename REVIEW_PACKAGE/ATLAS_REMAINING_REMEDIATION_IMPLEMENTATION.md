# Atlas — Remaining-Remediation Implementation (P2 round 3)

Round-3 delivered 6 additional High fixes on branch `p2-critical-high-remediation`:

| Commit | Findings | Change |
|---|---|---|
| P2.14a | F-021 | benchmark-relative walk-forward gate (`benchmark_folds` in all WF variants; `evaluate_approval` requires majority beat + fail-closed) |
| P2.16 | F-018 | `atlas/core/locks.py` canonical order; lifecycle lock takes audit lock first |
| P2.17a | F-024 | `evaluate_approval(mandatory_gates)`; failed kill is terminal; pead tool passes the failed kill |
| P2.18a | F-026 | `settle_orders` voids a stale non-authoritative buy fail-closed |
| P2.13 | F-014, F-015 | split-factor validation + nightly dividend refresh |

No schema migration was required this round (all behavioural + test-level; head
stays 0035). The open findings (F-002, F-006, F-007, F-009, F-010, F-012,
F-019, F-020, F-025, F-005-finish) each require their own increment — several
with new migrations (issuer identity, versioned bars, audit epoch, scheduler
cycle records) and, for F-012, a full strategy re-validation.

See `ATLAS_FINAL_REMEDIATION_EVIDENCE.md` for the complete finding table and
`ATLAS_OPERATOR_ACTIONS.md` for required human actions.
