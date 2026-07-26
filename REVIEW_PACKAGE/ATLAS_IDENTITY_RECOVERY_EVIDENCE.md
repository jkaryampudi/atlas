# Atlas — Identity Recovery Evidence (F-001 / F-002b / §6.3–6.4, §7)

**Question asked (§6.3):** does the repo already hold legitimate *dated* identity
evidence that is simply unwired, and if so, can it be wired/backfilled to raise
coverage toward the 90% floor — **without inventing dates, without ticker-only
fallback, and append-only (never a destructive overwrite)?**

**Answer:** **No.** An exhaustive search of the existing data found **zero** wireable
or backfillable identity evidence. The coverage gap cannot be closed from anything in
the repo. This document is the negative-result evidence, plus the fail-closed
regression matrix that guarantees the gate stays honest if such data is *never*
acquired.

**No production code and no data were changed by this recovery attempt.** There was
nothing legitimate to ingest; inventing anything would violate the task's own
constraints.

---

## 1. Search of existing data (§6.3) — measured results

All queries ran against the real Atlas DB (`main@d6d871f`), read-only.

### 1.1 Is there an unwired identifier anywhere?

> For every instrument with **no open identity row**, does any stored
> `market.fundamentals` payload carry an ISIN or CUSIP that was simply not picked up?

| Unresolved instruments with **any** stored payload | with an ISIN in payload | with a CUSIP in payload |
|---:|---:|---:|
| **0** | **0** | **0** |

**There is nothing to wire.** Every instrument that carries a resolvable permanent
identifier in existing data *already resolved*. The 171 unresolved panel members
resolve to nothing because the underlying identifier is genuinely absent — not because
a wiring step was skipped. (169 of the 171 have no fundamentals payload at all; the 2
that do carry neither ISIN nor CUSIP.)

### 1.2 Is there dated history to attest with?

`market.instrument_identity`: 526 rows, **all open** (`valid_to IS NULL`), **0** with
`history_complete = true`, **0** issuer-break versions ever recorded on real data,
`figi` column **0/526** populated. There is no dated symbol-change record, no issuer
validity interval, and no delisting history anywhere in the schema's populated
columns. Nothing to attest *from*.

### 1.3 Any second provider to cross-fill from?

`identity_source = eodhd_fundamentals` and every price bar's `source = EodhdAdapter`.
**Single provider.** There is no alternate feed already in the repo to reconcile
against.

**Conclusion of the search:** the maximum coverage reachable from existing data by
wiring everything wireable is **500 / 671 = 74.5%** — identical to today, because the
wireable set is empty. Reaching ≥ 90% (≥ 604) requires an **external dated feed**
(spec in `ATLAS_IDENTITY_DATA_REQUIREMENTS.md`).

---

## 2. Safe ingestion (§6.4) — nothing to ingest, and why that is correct

§6.4 authorises ingestion **only for available legitimate data**, append-only, never
overwriting, never inventing dates, leaving ambiguous cases quarantined, and failing
closed on missing dated history. Because §1 found **no** available legitimate dated
data, the correct action is to ingest **nothing**. Fabricating identifiers or back-
dating a current ISIN to unlock historical bars is exactly the false-continuity defect
F-001 exists to prevent.

What *is* already implemented and enforced (no change needed) is the safety envelope
that guarantees the outcome stays honest whether or not a feed ever arrives:

| §6.4 safety requirement | Mechanism (already in code) |
|---|---|
| Append / version, never destructive overwrite | `refresh_identity` closes the old era and opens a new one on issuer change; the old as-of still resolves to the old issuer. |
| Idempotent (re-run does not corrupt) | `populate_identities` re-run leaves exactly one open row. |
| Never invent dates | `valid_from` is pinned to the **first stored bar** (the vouchable floor), never fabricated. |
| Ambiguous stays quarantined | Duplicate symbol / missing identifier → `resolve_by_symbol` returns `None` (fail closed), not a guess. |
| Missing dated history fails **closed** | Single-snapshot pre-era bars dropped `unattested`; unresolved pre-era bars dropped; panel refuses below the coverage floor. |

---

## 3. Fail-closed regression matrix (proves the guarantees hold)

These tests already exist and pass; together they are the regression wall that keeps
the identity gate honest under the "no external feed" condition. **No gate was
weakened to make them pass; they assert the strict behaviour.**

| Scenario | Test | Asserted outcome |
|---|---|---|
| **Current-only ISIN must NOT unlock historical bars** (§7 core) | `test_pit_identity_gate_pg.py::test_single_snapshot_pre_era_dropped_fail_closed` | single open snapshot ⇒ all pre-era bars dropped `unattested`; only in-era bars survive. |
| Genuine attestation admits history | `…::test_attested_history_pre_era_is_kept` | `history_complete=true` ⇒ pre-era bars kept (gate is not blanket-conservative). |
| Reused ticker (different issuer) | `…::test_reused_ticker_pre_era_bars_are_dropped` | prior issuer's pre-era bars dropped `wrong_issuer`. |
| Unresolved member | `…::test_unresolved_pre_era_bars_fail_closed` | no identity ⇒ every pre-era bar dropped fail-closed. |
| Empty identity feed | `test_xsmom_pit_run_pg.py::test_identity_coverage_gate_refuses_ticker_only_panel` | panel **raises** rather than grade ticker-only. |
| Full coverage path | `…::test_identity_resolved_panel_reports_full_coverage` | resolved == total, no forced drops. |
| Break versions, not overwrites | `test_instrument_identity_pg.py::test_identity_break_versions_not_overwrites` | old era resolves to old issuer; one open-row invariant. |
| Idempotent populate | `…::test_populate_idempotent_single_open_row` | re-run ⇒ exactly one open row. |
| `valid_from` not fabricated | `…::test_resolves_isin_and_pins_first_bar_as_valid_from` | `valid_from` = first stored bar; `history_complete=false` reported honestly. |
| Ambiguous symbol | `…::test_ambiguous_symbol_fails_closed` | duplicate symbol ⇒ `None`. |
| Reused ticker before series | `…::test_reused_ticker_before_series_fails_closed` | stale-era as-of ⇒ `None`; in-series as-of ⇒ resolves. |
| Drift under a held position halts recon | `…::test_reconciliation_breaks_on_held_position_issuer_drift` | issuer reassignment ⇒ reconciliation BREAK. |

The §7 requirement — *"prove current-only identifiers do NOT unlock historical
formation bars"* — is met head-on by
`test_single_snapshot_pre_era_dropped_fail_closed`: the real-world vendor case (one
open row, `history_complete=false`, `valid_from` at first bar) drops **every** pre-era
bar. No new test was needed; the guarantee is already regression-locked.

---

## 4. ISIN backfill for the ~8 living names (§7) — not possible from repo data

The operator register (F-002b) flags "~8 living instruments (e.g. BNY)" for a targeted
ISIN backfill. Findings:

1. **The currently-active universe is already essentially fully identified** — of the
   active members, all but one resolve; there is no pool of active names missing an
   ISIN to backfill from existing data.
2. **No unresolved member carries an ISIN/CUSIP in any stored payload** (§1.1: 0/0/0).
   A legitimate backfill would require fetching identifiers from the vendor — an
   *external* action, out of scope here and not performed.
3. **Even a successful current-ISIN backfill would not move the failing windows.** The
   historical windows (2012–2022) fail because departed/delisted issuers had no
   identifier *at that time*. A living name's *current* ISIN is a single open snapshot —
   and `test_single_snapshot_pre_era_dropped_fail_closed` proves that snapshot cannot
   unlock the name's pre-identity-floor formation bars. So §7's backfill, even if
   externally performed, yields **zero** coverage gain against the historical shortfall
   that is causing the refusal.

**Therefore §7 is a no-op for the coverage floor from existing legitimate data.** The
correct action is to leave the data untouched and record the requirement in the
acquisition spec, which is done.

---

## 5. Net result

- Wireable identity evidence in the repo: **0**.
- Backfillable (from legitimate existing data): **0**.
- Coverage after exhausting all in-repo recovery: **74.5%**, unchanged, **< 90%**.
- Data invented or overwritten: **0** (nothing legitimate to ingest).
- Fail-closed guarantees: **regression-locked** by the matrix in §3.

The gate is correct and the data is genuinely missing. Recovery is **blocked on an
external dated feed** — see `ATLAS_IDENTITY_DATA_REQUIREMENTS.md`. Until then the
flagship remains **INSUFFICIENT EVIDENCE** and is not rerun.
