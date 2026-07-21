# Atlas — Post-Remediation Hostile Self-Review (P2)

An adversarial pass over the P2 increment: try to DISPROVE each remediation, and honestly re-state which review attack vectors remain OPEN. No new Critical/High defect was found in the code that was changed; the dominant residual risk is the large set of findings **not addressed** this increment.

## Attempts to break the fixes made

| Attack | Result |
|---|---|
| **Entry-day double-count still reachable?** (F-003) Same-day entry+exit; entry day-1 exit day-N; time/target exits | Marks once in every path. `test_f003_same_day_entry_and_exit_marks_from_entry` covers the pos_entry_i==i branch. Stash-revert makes the test fail → the fix is load-bearing. **Holds.** |
| **Stop fill better than obtainable?** (F-004) gap far through stop; open==stop; NaN/0/neg open | Fills at `min(stop, open)`; invalid open raises. Verified 7 cases incl. large gap to 50. **Holds.** |
| **Momentum overlay still dead?** (F-011) paper vs shadow vs uuid5 refs | Fires for paper, excluded for shadow; old MUTANT filter proven empty. **Holds.** Note: only binds when a momentum family is actually paper/live (none today). |
| **Future earnings still ingestible?** (F-008) future report_date; report_date==period_end; no known_as_of | Excluded at parse and store. **Holds for new ingestion.** OPEN: the 67 pre-existing rows are still in the DB (deferred). |
| **F-001 guard bypass?** straddling series; current-member null start; the exact ADT/VAL/MNK rows | Excludes zero-era series; admits straddling/current. Verified vs live ADT/VAL/MNK. **Holds for the unambiguous case.** OPEN: reused-ticker bars on a HELD position, multi-spell, delisted tails — need F-002. |
| **Replay still hits prod?** (F-017) name `atlas`/`production`; `--force` | Refused unless force. **Holds.** |
| **PG-less false green?** (M31) unreachable DB with the flag | Exit 4, not 0. **Holds.** |
| **Did any fix silently move validated numbers?** | The backtest goldens moved (F-003/F-004) and were re-pinned with old→new recorded and hand-calc justification; the PIT panel goldens did **not** move. No number changed silently. |
| **Did the changes break the two-plane wall / clock / audit invariants?** | `test_boundaries`, `test_clock`, audit tamper tests all still pass; no `datetime.now()` or agent import introduced. |

## Review attack vectors that remain OPEN (honest)

These are **not fixed** in this increment and remain exploitable exactly as the review described:

- **Unauthenticated trade approval (F-016):** any local process can still `POST /v1/trading/proposals/{id}/approve`. **OPEN.**
- **Secret leakage (F-013):** the EODHD key still travels in the URL and can still enter the audit chain / logs via `raise_for_status`. **OPEN — operator must rotate the key.**
- **Audit tail truncation (F-020) & hash coverage (F-019):** deleting the last N audit rows is still undetected; `entity_id`/`actor_id` still outside the hash. **OPEN.**
- **In-place bar/FX overwrite (F-007):** provider revisions still silently rewrite history; past runs still unreconstructable. **OPEN.**
- **DSR overstatement (F-005):** the `1/T` variance substitution still overstates the Deflated Sharpe. **OPEN.**
- **Currency-mismatched alpha (F-006):** AUD vs USD benchmark still differenced. **OPEN.**
- **WF gate not benchmark-relative (F-021); self-declared lineage (F-023); re-promotion legacy hole (F-022); pead-on-failed-kill (F-024).** **OPEN.**
- **Reused-ticker held-position marking / multi-spell (F-002, and thus the remainder of F-001).** **OPEN.**
- **Scheduler dead-man + durable failure records (F-025).** **OPEN.**

## New findings from this pass

None of Critical/High severity introduced by the P2 changes. One observation (Informational): the F-001 guard excludes zero-era series but does not yet *audit-log* the exclusion count in a durable place beyond the `PitExclusion` list in the run report — acceptable for now, worth surfacing in the scorecard later.

## Honest verdict on the increment

The six items closed are closed correctly and are regression-protected. **The completion gate (Critical/High = 0) is not met**: 1 Critical remains partially open (F-001, pending F-002) and ~14 High remain fully open. The system's research results are **not** made trustworthy by this increment — the backtest arithmetic and one PIT contamination are corrected (real improvements to credibility), but data-integrity, reproducibility, statistical-honesty, and security defects that drove the "NOT YET TRUSTWORTHY" verdict are still substantially open.

---

## Round-2 hostile checks

| Attack | Result |
|---|---|
| **Secret in an adapter error?** (F-013) canary token on HTTP-status + connect-error paths | Scrubbed; `__cause__` dropped. **Holds.** OPEN: historical leaked key must be rotated. |
| **Unauthenticated mutation?** (F-016) approve/settle without/with wrong/valid token | 401/403/503/pass. **Holds** for the 5 trading endpoints. OPEN: other mutating routers (factory burn, scheduler) still unauthenticated. |
| **Fresh lineage resets the DSR penalty?** (F-023) `register_trial(lineage='special-tag')` | Refused pre-INSERT. **Holds.** |
| **Promote on an unstamped/mismatched artifact?** (F-022) never-shadowed strategy, no `_identity` / wrong `_identity` | Refused; matching stamp promotes. **Holds.** |
| **DSR still uses 1/T?** (F-005) | The estimator variance is corrected everywhere; the expected-max dispersion is correct only when the caller supplies it — **still the fallback by default**. PARTIAL. |

**Newly-OPEN-and-confirmed still-exploitable:** audit tail-truncation (F-020, probed), in-place bar overwrite (F-007), currency-mismatched alpha (F-006), reused-ticker held-position marking (F-002), WF gate not benchmark-relative (F-021). These remain as the reviewer described.
