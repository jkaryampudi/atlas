# Atlas — Identity Data Requirements (external acquisition spec)

**Status:** BLOCKING for the definitive backtest. **Owner:** Principal (vendor
procurement). **Trigger:** the identity coverage gate cannot reach `IDENTITY_COVERAGE
_FLOOR = 0.9` from any data in the repo (measured 74.5%; see
`ATLAS_IDENTITY_COVERAGE_ANALYSIS.md` and `ATLAS_IDENTITY_RECOVERY_EVIDENCE.md`).

This is the acquisition specification for the **external dated identity feed** that
would legitimately close the gap. It exists so the vendor decision is made against a
concrete, testable requirement — not a vague "we need better data."

Nothing here authorises a purchase, and no feed was acquired. This is a specification,
not an action.

---

## 1. Why an external feed is required

The single current provider (EODHD) serves fundamentals — hence identifiers — only for
**currently-listed** issuers. The coverage gap is entirely **departed / delisted /
symbol-changed** members in the historical windows (2012–2022 all < 90%; see the
coverage analysis §3). No amount of re-fetching the current snapshot reaches those
issuers. A **point-in-time security-master / issuer-history** feed is the only
legitimate source.

Two independent problems must both be solved:

1. **Resolution** — give the 171 unresolved members a permanent identifier valid at
   their formation dates (lifts 74.5% → ≥ 90%).
2. **Attestation** — give resolved members a dated change-history so `history_complete`
   can be set truthfully (restores pre-membership formation bars that are currently
   dropped `unattested`; see coverage analysis §5).

A feed that solves only (1) clears the coverage floor but leaves recently-added names
formation-starved. Prefer a feed that solves both.

---

## 2. Required data items (the minimum viable feed)

Each row must be **dated** (carry validity intervals), cover **departed** issuers, and
be **reproducible** (a stable vendor key). Eleven required items:

| # | Item | Shape | Why it is needed |
|---|---|---|---|
| 1 | Dated symbol-change history | `(old_symbol, new_symbol, effective_date)` | Reconstruct a ticker's lineage at any as-of date. |
| 2 | Issuer/security-master validity intervals | `(security_id, issuer_id, valid_from, valid_to)` | The spine that makes identity bitemporal; lets `history_complete` be set. |
| 3 | ISIN history with dates | `(security_id, isin, valid_from, valid_to)` | Identifiers are reassigned; a bare current ISIN is not enough. |
| 4 | CUSIP history with dates | `(security_id, cusip, valid_from, valid_to)` | Secondary key; US coverage. |
| 5 | SEDOL history with dates | `(security_id, sedol, valid_from, valid_to)` | Cross-vendor reconciliation; non-US readiness. |
| 6 | FIGI (OpenFIGI) with dates | `(security_id, figi, valid_from, valid_to)` | Provider-permanent anchor that survives symbol changes (`figi` column exists, 0/526 today). |
| 7 | Vendor permanent security ID | `(security_id, vendor_permid)` | The stable spine to join items 1–6 across time. |
| 8 | Delisting history | `(security_id, delist_date, reason)` | Reaches the 73 delisted members — unreachable from current fundamentals. |
| 9 | Corporate-action history | `(security_id, event_type, effective_date, counterparty_security_id)` | Merger / acquisition / spin-off / ticker-reuse linkage. |
| 10 | Issuer (LEI / entity) linkage | `(security_id, issuer_lei, valid_from, valid_to)` | Distinguishes same-ticker different-issuer (the F-001 defect). |
| 11 | Coverage of **departed** index members | full historical S&P 500 constituent set, not just current | The entire gap is issuers no longer in the book. |

**Items 8, 9 and 11 are the binding constraints.** They are exactly what a
current-fundamentals vendor cannot serve and exactly what the failing windows need.

---

## 3. Coverage & quality bar (acceptance criteria)

A candidate feed is acceptable only if, when ingested through the *existing* append-
only / fail-closed identity layer (no code weakening), it produces:

1. **Panel coverage ≥ 90%** on the full definitive window (`WINDOW_START = 2012-07-01`),
   i.e. ≥ 604 of 671 candidates resolve **at their formation as-of dates** — not merely
   today.
2. **Per-window coverage ≥ 90%** for every rebalance window 2012→2026 (today 2012–2022
   all fail; see coverage analysis §3).
3. **Attestation available** — `history_complete` can be set truthfully for members
   with a genuine dated change-history, so pre-membership formation bars stop being
   dropped `unattested`.
4. **Departed-issuer coverage** — resolves the 73 delisted + the departed living
   members, with validity dates.
5. **Dated, not snapshot** — every identifier carries `valid_from`/`valid_to`; a bare
   current identifier is rejected as non-attesting.

---

## 4. Candidate vendor classes (illustrative, not an endorsement)

Point-in-time security-master / reference-data providers that historically serve
dated identifier + corporate-action history for **departed** issuers:

- Institutional security-master / reference-data feeds with PIT symbology and
  corporate-action history.
- OpenFIGI (Bloomberg) for FIGI anchoring (item 6) — pairs with a symbology-history
  source for the dates.
- A dated symbol-change + delisting dataset (survivorship-bias-free index constituent
  history) for items 1, 8, 11.

The specific vendor is a **Principal procurement decision**. This spec is vendor-
neutral; acceptance is by §3, measured through the existing gate.

---

## 5. Ingestion contract (must hold when a feed arrives)

The feed must ingest through the **current** identity layer with **no gate weakening**:

- **Append-only / versioned** — new eras close old eras; old as-of still resolves to
  old issuer (`refresh_identity` semantics; `test_identity_break_versions_not_overwrites`).
- **No invented dates** — every `valid_from` comes from the feed or the first stored
  bar; never fabricated.
- **Ambiguity quarantined** — conflicting rows fail closed to `None`, not a guess.
- **Fail closed on gaps** — a member the feed does not cover stays unresolved and is
  dropped, not admitted on ticker.
- **Idempotent** — re-ingest leaves one open row per era.

If a feed cannot satisfy §3 through this contract, it is rejected — the correct outcome
stays INSUFFICIENT EVIDENCE rather than a coverage number bought by loosening the gate.

---

## 6. Until then

The flagship `xsmom-pit-tr` remains **`research_shadow`** (ADR-0018) and, on rerun,
returns **INSUFFICIENT EVIDENCE** (coverage 74.5% < 90%). Re-promotion out of shadow
requires a fresh signed validation artifact (ADR-0018), which requires the definitive
backtest to run, which requires this feed. The dependency chain is explicit and
fail-closed.
