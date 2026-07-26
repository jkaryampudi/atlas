# Atlas — Identity Coverage Analysis (F-001)

**Scope:** the definitive PIT panel's issuer-identity coverage on the *real* Atlas
database (`main@d6d871f`), and the minimum dated data required to reach the
`IDENTITY_COVERAGE_FLOOR = 0.9` gate honestly. **No strategy math, weights, floors,
or gates were changed to produce this analysis.** All numbers are measured, not
asserted.

This is a **missing-data** finding, not a bug. The identity gate is behaving exactly
as designed: it refuses to grade the flagship on ticker-only history, and it fails
*closed* (INSUFFICIENT EVIDENCE) rather than admitting unvouchable bars.

---

## 1. Headline

| Metric | Value |
|---|---|
| Panel candidates (window members with price bars) | **671** |
| Resolved to an issuer identity (panel path, PIT as-of) | **500 (74.5%)** |
| Unresolved | **171 (25.5%)** |
| Coverage floor required to run the definitive backtest | **90.0%** (≥ 604 of 671) |
| Shortfall to floor | **104 members** |
| **Verdict** | **BELOW FLOOR → panel refuses → INSUFFICIENT EVIDENCE** |

The strategy does not run. `load_pit_panel` raises on coverage below the floor
(`tests/integration/test_xsmom_pit_run_pg.py::test_identity_coverage_gate_refuses_ticker_only_panel`).

---

## 2. Why the members are unresolved (reason breakdown)

Of the **171** unresolved panel candidates:

| Reason | Count | Meaning |
|---|---:|---|
| Delisted, no identity | **73** | Left the index and delisted; EODHD serves no fundamentals → no ISIN/CUSIP → no identity row. |
| Living, no identity | **98** | A company still exists but the ticker/instrument row has no resolvable permanent identifier in the repo (departed the index, changed symbol, moved venue, or was acquired-and-relisted). |
| — of which carry an unwired ISIN/CUSIP in a stored payload | **0** | *There is nothing to wire.* No unresolved instrument has an ISIN or CUSIP sitting in a fundamentals payload that simply wasn't picked up. |

**Attestation is separately zero.** Of the 500 that *do* resolve, **0** attest their
pre-membership history (`history_complete = true` on 0 of 526 identity rows). Every
identity is a *single open snapshot* — one row, `valid_to IS NULL`, `history_complete
= false`. That means even a resolved current member cannot vouch its own pre-index
formation bars (see §5), so resolution ≥ floor is necessary but not sufficient for a
clean run.

---

## 3. The coverage gap is historical (survivorship-shaped)

Coverage is a function of *how far back the rebalance window reaches*. The current
book resolves almost perfectly; the historical windows do not, because they include
members that have since departed/delisted and for which EODHD (a current-fundamentals
vendor) serves no identifier.

| Rebalance window | Resolved / eligible | Coverage | Gate |
|---|---:|---:|:--|
| 2012-07-31 | 290 / 339 | **85.5%** | ✗ < 90 |
| 2014-06-30 | 313 / 373 | **83.9%** | ✗ < 90 |
| 2016-06-30 | 343 / 415 | **82.7%** | ✗ < 90 |
| 2018-06-29 | 380 / 452 | **84.1%** | ✗ < 90 |
| 2020-06-30 | 414 / 479 | **86.4%** | ✗ < 90 |
| 2022-06-30 | 432 / 495 | **87.3%** | ✗ < 90 |
| 2024-06-28 | 463 / 501 | **92.4%** | ✓ |
| 2026-06-30 | 499 / 502 | **99.4%** | ✓ |

**Reading:** every window from 2012 through 2022 is below the floor; only 2024–2026
clears it. The definitive backtest (`WINDOW_START = 2012-07-01`) must span all of
these, so the *full-window* coverage (74.5%) is dominated by the historical shortfall.
The gap is not noise — it is a monotone survivorship signature: the further back you
look, the more departed issuers you need and the fewer the current vendor can identify.

---

## 4. Coverage by market and provider

| Cut | Value |
|---|---|
| **Market** | US only — 500 / 671 = 74.5%. (Universe is 511 USD + 1 AUD; no non-US member reaches the ranked panel.) |
| **Bar / fundamentals provider** | **EodhdAdapter is the only provider.** Every price bar and every identity (`identity_source = eodhd_fundamentals`) traces to one vendor. There is no second feed to cross-fill from. |

Single-provider dependence is the structural cause: EODHD's fundamentals endpoint
returns identifiers for *currently-listed* issuers. Delisted/departed members return
nothing, so they can never resolve from this source at any price.

---

## 5. Identifier population (what the identity rows actually hold)

`market.instrument_identity` — 526 rows, all open (0 closed / 0 versioned breaks):

| Column | Populated | Note |
|---|---:|---|
| `isin` | 518 / 526 | Primary resolution key. |
| `cusip` | 509 / 526 | Secondary key. |
| `figi` | **0 / 526** | Column exists; **never populated** (EODHD does not serve OpenFIGI). |
| `history_complete` | **0 / 526** | **No identity attests its history.** All single-snapshot. |
| `valid_to IS NULL` (open) | 526 / 526 | No issuer-break versioning has ever fired on real data. |

Consequence for the momentum signal: with `history_complete = false` everywhere, the
per-bar gate `admit_pre_era_bars_by_issuer` marks every *pre-membership* formation bar
`unattested` and drops it fail-closed. A current member's *current* ISIN never
silently vouches the ticker's *earlier* history — the exact false-continuity defect
F-001 closed. So even at ≥ 90% resolution, the 12-1 formation would be starved for
recently-added names until they season 252 in-index sessions. **Resolution raises the
coverage number; only attestation restores the formation history.** Both need dated
data the repo does not have.

---

## 6. Minimum data required to reach the floor honestly (§6.2)

To lift coverage to ≥ 90% *and* restore formation history without inventing anything,
the repo needs **dated, point-in-time issuer/security-master history** — not more of
the current-snapshot feed. The minimal set:

1. **Dated symbol-change history** — `(old_symbol, new_symbol, effective_date)` so a
   ticker's lineage is reconstructable at any as-of date.
2. **Issuer / security-master history with validity intervals** — `(security_id,
   issuer_id, valid_from, valid_to)`; the spine that turns a snapshot into a bitemporal
   record and lets `history_complete` be set truthfully.
3. **Permanent-identifier history** — ISIN / CUSIP / SEDOL / FIGI **with their own
   validity dates** (identifiers are reassigned; a bare current ISIN is not enough).
4. **Provider permanent IDs** — a vendor-stable security key (e.g. FIGI, or the
   vendor's own permID) that survives symbol changes, to anchor the lineage.
5. **Delisting / corporate-action history** — `(security_id, event, effective_date)`
   for delist, merger, acquisition, spin-off, ticker reuse — the 73 delisted + departed
   living members are unreachable without this.
6. **Coverage for departed members**, not just the current index — the entire gap is
   issuers *no longer* in the book.

Item (5)+(6) are the binding constraints: they are precisely what a
current-fundamentals vendor cannot serve, and precisely what the historical windows
need.

The acquisition specification for such a feed is in
**`ATLAS_IDENTITY_DATA_REQUIREMENTS.md`**. The evidence that none of it exists in the
repo today (and that nothing can be safely wired or backfilled from what is present) is
in **`ATLAS_IDENTITY_RECOVERY_EVIDENCE.md`**.

---

## 7. What was *not* done (guardrails held)

- `IDENTITY_COVERAGE_FLOOR` remains **0.9**. Not lowered.
- No ticker-only fallback was added. The panel still refuses below floor.
- No dates were invented; no placeholder identifiers were created; no ambiguous
  instrument was force-resolved.
- No strategy weights, thresholds, or gates were touched.

**Bottom line:** coverage is **74.5% < 90%**, the shortfall is real historical
missing-data (not a wiring bug), and it is unreachable from any data currently in the
repo. The correct, designed outcome is **INSUFFICIENT EVIDENCE** — no rerun.
