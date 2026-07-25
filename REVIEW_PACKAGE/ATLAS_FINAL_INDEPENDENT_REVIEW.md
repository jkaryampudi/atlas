# Atlas Existing Application — Final Independent Remediation Review

> **CLOSURE (P2.30–P2.36, branch `p2-critical-high-remediation`):** every finding
> this review returned as REQUEST CHANGES — F-001, F-006, F-010, F-013, F-016,
> F-019, F-020 — is now **FIXED through the real production path**, with the bypass
> reproduced and a regression test. Completion gate met (unresolved Critical/High
> = 0); full `pytest` green from a from-scratch DB, ruff + mypy clean. See
> `ATLAS_FINAL_GATE_REMEDIATION.md`, `ATLAS_FINAL_GATE_EVIDENCE.md`, and the
> 23-vector `ATLAS_FINAL_GATE_SELF_REVIEW.md`. The read-only findings below are
> preserved verbatim as the record of what was wrong.

**Read-only, hostile.** Verdict is based on the ACTUAL production decision/write paths on the current
branch, not on the remediation report. Where a "FIXED" label rested on a mechanism that is not wired
into the real path, it is marked PARTIAL or OPEN. No production code was altered.

---

## 1. Repository, branch and commit reviewed

| Item | Value |
|---|---|
| Repo | `~/Documents/atlas` (Atlas AI Capital — the EXISTING app, not Atlas Next) |
| Branch | `p2-critical-high-remediation` |
| HEAD | `aa7c01b` (`docs: record the Critical/High re-verification …`) |
| Worktree | **clean** (no uncommitted changes) |
| Named commits | all present: `50e1e14` (F-002), `d416ac2` (F-006/F-010), `b000b57` (F-019/F-020), `8949901` (F-001), `fbbd199` (P1.4 substrate); plus `7fd676a` (fbbd199 hostile-review fixes) and the earlier remediation chain (F-005 `3d90465`, F-007 `6fba600`, F-009 `d75deb2`, F-025 `0bc7fde`, F-012 `2b28e4a`, M35 `31efb7a`, …) |
| Migration head | `0042` (`0042_audit_append_only_and_monotonic_anchor.py`) |

---

## 2. Commands and actual results (verbatim)

All run from a **completely absent** disposable PostgreSQL database (`DROP DATABASE atlas_test`), no
manual repair.

| Command | Result | Exit |
|---|---|---|
| `pytest` (from absent `atlas_test`) | **1773 passed, 0 failed, 0 skipped, 1 warning** (StarletteDeprecationWarning, benign); ~98 s | **0** |
| `ruff check .` | All checks passed | 0 |
| `mypy` | Success: no issues in 138 source files (strict on core/dcp/fxlab) | 0 |
| `make doctor` | all clear — python 3.14.4, venv active, `atlas-db-1` up, PG reachable (48 tables), EODHD key present | 0 |
| `make verify-chain` | audit chain OK: **2338 event(s) verified** (dev `atlas` chain) | 0 |
| `make cov-risk` | `dcp/risk` **100.00% branch coverage** (engine/factor_overlap/stress/vol_target/seed_limits all 100%); "Required test coverage of 100% reached" | 0 |
| Migration round-trip (fresh `atlas_test_migchk`) | `upgrade head`→`0042` ✓; `downgrade 0041` ✓; `downgrade base` ✓ (full unwind 0001→base); `upgrade head`→`0042` ✓ | 0/0/0/0 |
| **Zero-skip guard** — `ATLAS_REQUIRE_PG=1` + unreachable PG (`:5599`) | conftest `pytest_configure` raises `UsageError`: "Refusing to report a green run that tested nothing structural" | **4** (hard fail, no silent skip) |

**Clean-DB provisioning is automatic and required no manual `DROP`.** conftest `_ensure_test_db`
(`tests/conftest.py:103-172`) now measures worst-case per-table `pg_attribute` count and proactively
`DROP+recreate`s before `alembic upgrade` when it exceeds `_COLUMN_PRESSURE_LIMIT = 1200`, plus a
reactive on-failure rebuild — so the earlier "column-budget rot" cascade cannot recur.

---

## 3. Findings by severity (this review)

**Open code-level gaps behind a prior "FIXED"/"closed" label (the material findings):**
- **F-016 (HIGH, security)** — state-mutating endpoints outside the trading router are **unauthenticated**.
- **F-013 (HIGH, security)** — two EODHD endpoints leak the API token (bypass the redactor).
- **F-006 (HIGH)** — the memo scorecard benchmark-relative excess is still currency-inconsistent.
- **F-010 (HIGH)** — `financials_panel.fcf_yield_pct` still mixes statement/listing currency.
- **F-001 (CRITICAL, data-blocked)** — a pre-index wrong-issuer head splice can still enter the definitive PIT panel.

**Application-level closures with a fully-privileged-DBA residual:**
- **F-019 / F-020 (HIGH)** — the append-only + monotonic audit guards are enforced for a normal app role, but a **superuser/table-owner** can bypass them.

Everything else previously reported (see §4) is genuinely closed in the production path.

---

## 4. Per-finding closure verdict

Status vocabulary: **FIXED | PARTIAL | OPEN | NOT REPRODUCIBLE**.

### The six latest fixes (re-attacked with concrete vectors)

| Finding | Status | Basis |
|---|---|---|
| **F-002** issuer identity (write path) | **FIXED** (with F-001-shared residual) | `refresh_identity` **versions** on an issuer-key change — old era keeps its old ISIN, one OPEN row, `issuer_break_count` durable (`identity.py:267-297`; `test_instrument_identity_pg.py:155-179`). Held-position drift is wired into the real T6 reconciliation and **halts the book** (`ops/daily.py:256-277`; `test:201-228`). Resolver fail-closes on unresolved/ambiguous/before-floor. Residual (below): the physical BAR write is not identity-guarded. |
| **F-001** wrong-issuer PIT contamination | **PARTIAL** (Critical, data-blocked) | Reviewed cases closed: post-`end_date` bars clipped off the panel (`xsmom_pit_run.py:549,565`, `clip_after_membership_end`), zero-overlap reused-ticker (ADT/VAL/MNK) refused. **BUT** the panel never consults `instrument_identity` (grep: zero consumers in `backtest/*`, `real_run.py`, `total_return.py`), and `clip_after_membership_end` keeps EVERYTHING for an OPEN membership (`end_date=None`). A current member whose UUID carries a prior issuer's **pre-`start_date`** bars (reused ticker) feeds those wrong-issuer prices into its momentum-formation lookback. **Per the task's own test — if missing dated vendor history can still admit wrong-issuer data into the definitive panel, F-001 is NOT closed — it can. F-001 CANNOT be reclassified Critical→Medium.** |
| **F-006** AUD benchmark consistency | **PARTIAL** | The capital/governance DECISION paths are FIXED: demotion band (`bands._spy_tr_close` → AUD via `fx_to_aud`, `bands.py:186`) and attribution (booked `*_aud` amounts). Walk-forward folds/approval gates compare same-panel same-currency series. **Residual:** the memo **scorecard** grades `excess = fwd_return − spy_return` as raw price-return ratios (`scorecard.py:50-52`) — instrument-currency vs USD — so for any non-USD-listed name the excess is currency-inconsistent. Latent while the scored universe is USD-listed; overlaps the pre-existing M21 medium. |
| **F-010** ADR/currency mismatch | **PARTIAL** | `valuation_models.compute_valuation` now fails closed on a reporting-vs-listing mismatch (`valuation_models.py` `_currency_blocked`), and `health_score` fail-closes `fcf_yield`. **Residual:** `financials_panel._key_stats` computes `fcf_yield_pct = 100*fcf_ttm/market_cap` (`financials_panel.py:234`) — statement-currency FCF ÷ listing-currency market cap, **no guard, no fail-closed** — and it is surfaced in the production dossier response. A third ratio path was missed. |
| **F-019** audit identity integrity | **FIXED** (app level) + DBA residual | Migration 0042 `decision_events_append_only` BEFORE UPDATE/DELETE trigger refuses **every** row mutation unconditionally. Empirically verified as the app role: UPDATE `actor_id`/`entity_id`/`payload`/`prev_hash` and DELETE all **REFUSED** (`RaiseException … append-only`), rows intact, on both legacy (`hash_version=1`) and epoch-2 rows. Residual §8. |
| **F-020** audit tail integrity | **FIXED** (app level) + DBA residual | `chain_head` guard now refuses any UPDATE that lowers `last_seq`/`event_count` and any DELETE **even with the writer GUC set**; `audit_repo` advances `event_count` by a monotonic `+1` (never `count(*)`), so no self-heal. Empirically: as an app-role writer, delete-last / delete-several / bulk-delete of events all **REFUSED**; anchor lower/rollback/delete refused. Residual §8. |

### Previously-closed findings — regression re-verification

| Finding | Status | Production-path evidence |
|---|---|---|
| F-003 entry-day double-count | **FIXED** | `engine.py` unified `>=`; hand-calc goldens |
| F-004 optimistic stop fill | **FIXED** | `stop = min(stop, open)`; gap goldens |
| F-005 DSR 1/T substitution | **FIXED** | empirical `lineage_sr_dispersion` threaded into `portfolio_gate`/`null_model_gate` + all 8 runners with the fail-closed floor; flagship honest **DSR 0.752 < 0.90** (disclosed) |
| F-007 bar/corp-action versioning | **FIXED** | bitemporal append-only + immutability triggers (0040/0041); `set_knowledge_date` GUC set at t0 on every ingest path; byte-identical replay test. Residual: default reads are head (K-pinning opt-in) |
| F-008 future-dated earnings | **FIXED** | parse+store guard refuses `report_date > fetch` |
| F-009 mixed split-basis | **FIXED** (data residual) | `split_basis_asof` (0038) + read-side rebasing wired into SUE/PEAD; today's single-fetch panel → factor 1 (preventive) |
| F-011 §12 overlay sentinel | **FIXED** | `MUTANT_` demoted to a comment; live `st.state IN ('paper','live')`; overlay bound to the verdict (`proposals.py:637,705,757`) |
| F-012 monthly rebalance-sell | **FIXED** | `scan_rebalance_exits` full-exits winner-set dropouts; node `t6d_rebalance` (`daily.py:446`) |
| **F-013** EODHD secret leak | **PARTIAL** | `_get` wraps calls in `RedactingError` (`eodhd.py:98-102`), BUT `fetch_earnings_calendar` (`eodhd.py:163-168`) and `fetch_fundamentals` (`eodhd.py:190-192`) call `self._client.get()`+`raise_for_status()` **directly** — no redaction — and the token is a query param, so an HTTP error leaks it in the httpx exception, which is folded into audit/alert failure strings. Live repro (independent): both endpoints raise with the token present. `test_secret_redaction.py` only exercises `_get`. |
| F-014 bad split factor | **FIXED** | `is_valid_split_ratio` quarantine; `record_split` is the sole chokepoint |
| F-015 dividend decay | **FIXED** | nightly `fetch_dividends`+`record_dividend` in `_ingest_market` |
| **F-016** API authentication | **PARTIAL** | Trading mutators all carry `Depends(require_api_auth)` (`trading.py:145,172,190,228,265`). **BUT** `require_api_auth` is not even imported in `system.py`/`research.py`/`factory.py`, and `risk.py` `/breaker-clearances` + `/breaker-clearances/{id}/confirm` (`risk.py:135,148` — **Confirmation B that clears a latched DD2/DD3 drawdown breaker**), `system.py:70` `/run-daily` (fires the whole autonomous T0–T9 cycle), `factory.py:79` `/recipes/run`, and the `research.py` mutators are all **unauthenticated**. `main.py` adds no router-level dependency; `test_api_auth.py` only touches a GET health route. |
| F-017 replay DB guard | **FIXED** | `assert_disposable_db` refuses non-`*_test` DSN before any write; `--force` documented |
| F-018 lock ABBA | **FIXED** | canonical AUDIT(1)→LIFECYCLE(2) order; no path inverts |
| F-021 WF benchmark gate | **FIXED** | fold success = excess-over-benchmark |
| F-022 re-promotion hole | **FIXED** | unconditional identity/freshness |
| F-023 lineage catalog | **FIXED** | bound at the registration chokepoint |
| F-024 failed-kill approval | **FIXED** | `evaluate_approval` refuses on any false mandatory gate + unconditional `gate.passed` |
| F-025 scheduler reliability | **FIXED** | `ops.cycle_runs` durable ledger (0039) on an autonomous txn; dead-man/stuck supervision; spend captured before rollback |
| F-026 stale-order settle | **FIXED** | settle refuses a non-authoritative buy, deploys no capital |
| M31 zero-skip | **FIXED** | `ATLAS_REQUIRE_PG=1` → `UsageError` exit 4 (verified); set in CI |

---

## 5. Production-path enforcement evidence (highlights)

- **Enforced in the real path:** F-002 identity break-versioning + T6 reconciliation halt; F-005 DSR
  dispersion in the binding approval gate + all runners; F-007 `set_knowledge_date` before every ingest
  write; F-012 `t6d_rebalance` cycle node; F-018 lock order at every book-mutating entrypoint; F-019/F-020
  DB triggers on the append path; F-024 mandatory-gate + `gate.passed`; F-026 in `settle_orders`; M31 in
  `pytest_configure`.
- **NOT reaching the real path (the gaps):** F-001 — `instrument_identity` has **zero consumers** on the
  validation-panel builder (`xsmom_pit_run.py`/`real_run.py` join by symbol only); F-013 — two adapter
  endpoints bypass the redactor; F-016 — auth dependency absent from 4 routers' mutators; F-006 scorecard
  / F-010 `financials_panel` — the convert/fail-closed pattern was applied to sibling modules but not these.

---

## 6. Migration and clean-database evidence

- Head `0042`; full chain applies from an absent DB with no manual step (§2).
- **0042 round-trips**: `upgrade head` → `downgrade 0041` → `downgrade base` → `upgrade head`, all exit 0,
  landing on `0042 (head)`.
- Proactive column-pressure rebuild verified to fire (independent test: a seeded 1300-column table
  triggered an unattended `DROP`+recreate; DB returned to head, max cols 25). One residual: no *committed*
  unit test pins the rebuild mechanism (the demonstration was manual) — minor test-coverage gap.

---

## 7. Reproducibility assessment

- **Interpreter pin**: `requires-python >= 3.14` was **technically unnecessary** (no 3.13/3.14-only syntax;
  `atlas/` compiles on 3.12) and was correctly reverted to `>= 3.12` (pyproject, uv.lock, Dockerfile, mypy
  all `3.12`), with the exact dev/CI RUNTIME pinned separately in `.python-version = 3.14.4` + CI setup-uv.
  Internally coherent (contract floor vs runtime pin).
- **uv.lock**: `uv lock --check` clean; exports pinned+hashed requirements; CI runs `uv sync --locked`.
- **AST wall-clock guard**: catches aliased (`dt.now()`), aliased-module (`d.datetime.utcnow()`),
  module-qualified (`datetime.datetime.now()`), date, and `time.time()`/bare-`time()` spellings (self-test,
  8 flagged / 5 seams safe). `getattr`/`eval` dynamic access + SQL `now()`/`CURRENT_DATE` are **honest,
  documented** static-analysis limits, not silent holes.
- **Definitive replay spine**: `make replay` and `daily.py --now` thread a `FrozenClock` and set the
  `atlas.knowledge_date` GUC before any versioned write, so the bitemporal triggers never hit their `now()`
  fallback — the deterministic spine cannot silently fall back to wall time.
- **Residual (infra, not a decision-path defect)**: the Docker `api` image builds from `python:3.12-slim`
  and installs via `pip install -e .` — so the container reproduces neither the pinned 3.14.4 interpreter
  nor `uv.lock` (the lock is load-bearing in CI only).
- **Residual (subsystem, documented)**: the non-deterministic desk/LLM subsystem reads SQL `CURRENT_DATE`
  (`budget.py:20`, `research.py`) and `agent_runs.created_at DEFAULT now()`; a keyed `daily --now` stamps
  those off the DB clock — outside the `make replay` gate=green envelope.

---

## 8. External-data and infrastructure conditions (NOT code defects)

1. **Fully-privileged DBA / superuser threat (F-019/F-020 residual).** The DB triggers stop a normal app
   role, but the app connects as `atlas`, which is confirmed **superuser + table owner** (`pg_roles.rolsuper=t`).
   A superuser can `SET session_replication_role='replica'` (independently verified: the tampering UPDATE
   then **succeeds**) or `ALTER TABLE … DISABLE TRIGGER`, or `TRUNCATE` both tables. **Condition:** run the
   production app under a NON-superuser, non-owner role; and add an external WORM/signed anchor for
   defence-in-depth.
2. **Dated vendor symbol-change / delisting history (F-001/F-002 residual).** Distinguishing a same-issuer
   pre-index bar from a reused-ticker's requires a vendor symbol-change feed not ingested (EODHD serves one
   current snapshot). Until procured, the pre-index head-splice vector stays open (data-blocked).
3. **Physical historical series split** for reassigned tickers, **pre-cutover bar knowledge-time**
   (unrecoverable), **FX versioning** — documented data/scope residuals.
4. **EODHD credential rotation** (the key already leaked historically; F-013 additionally still leaks it on
   two live paths — see §4).
5. **Stale-order operator cleanup**, **production API auth token + alert URL configuration** — operator
   actions (and F-016 must be closed in code first).

---

## 9. Updated point-in-time verdict — **PASS WITH CONDITIONS (issuer-level PIT: PARTIAL)**

Date-interval membership guards, zero-overlap reused-ticker exclusion, post-`end_date` clipping, and
bitemporal bar/corp-action versioning (F-007) are solid and enforced. **But** issuer-level PIT correctness
is not closed: the definitive panel does not consult `instrument_identity`, so a reused-ticker's
pre-index bars can still contaminate a current member's momentum formation (F-001). This is data-blocked
on the vendor feed, not a code oversight — but it remains a live Critical-severity PIT gap.

## 10. Updated backtest-credibility verdict — **CONDITIONAL / improved**

Arithmetic (F-003/F-004), statistics (F-005 honest **DSR 0.752 < 0.90**, F-021/F-022/F-023), trial-count
discipline, and byte-identical replay (F-007) are genuinely fixed and honestly disclosed. The flagship
`xsmom-pit-tr` is `research_shadow` (no capital). Residuals that cap credibility: the F-001 pre-index
wrong-issuer contamination and the F-006 scorecard currency inconsistency (latent on the USD universe).
Credible for a below-gate, non-authoritative research artifact; not yet a clean decision-grade panel.

## 11. Updated paper-trading-readiness verdict — **NOT READY (control gaps)**

The trading write path is well-controlled (F-016-trading auth, F-018 locks, F-024 gate, F-026 stale-order,
the M35 price collar, reconciliation-as-kill). **But two real control gaps remain even in paper mode:**
F-016 leaves the **risk-breaker-clearance/confirm** (clears a drawdown breaker) and **/run-daily** (fires
the autonomous cycle) **unauthenticated** on the loopback API, and F-013 leaks the vendor token into
audit/alerts. Close F-016 and F-013 before treating the paper loop as safe to expose.

## 12. Updated real-capital-readiness verdict — **NOT READY**

Phase 7 is gated by design, and correctly so. Blockers beyond the phase gate: F-016 (auth), F-013 (secret),
F-001 (PIT), the superuser DB role and absent external audit anchor (§8), plus credential rotation and
production auth/alert configuration. Real capital must not be armed on this branch.

## 13. Overall research-trustworthiness verdict — **CONDITIONALLY TRUSTWORTHY (research only)**

The branch is a large, honest improvement: the backtest arithmetic/statistics, audit-chain integrity (for
a non-superuser role), reproducibility substrate, and the identity/currency **decision** paths are
genuinely enforced and disclosed (including the below-gate DSR). Trustworthy enough for **measured,
non-authoritative research**. It is NOT trustworthy for capital while the open code gaps (F-013, F-016,
F-006, F-010) and the Critical data-blocked F-001 remain.

---

## Final gate verdict

```
REQUEST CHANGES
```

**Rationale.** F-013 (token leak on two live adapter endpoints) and F-016 (unauthenticated
risk-breaker-clearance and autonomous-cycle trigger) are **HIGH, security, and CODE-fixable** — they are
not external conditions, and they sit behind "FIXED" labels. F-006 and F-010 are HIGH partials with a
concrete unguarded path each. These require code changes before merge. F-001 (Critical) is genuinely
data-blocked (external condition) but, per the task's stated criterion, is NOT closed. Once F-013/F-016 are
closed in code and F-006/F-010 completed, the branch would move to **APPROVE FOR MERGE WITH EXTERNAL
CONDITIONS** (F-001 vendor feed, F-019/F-020 non-superuser role + external anchor, credential rotation,
production auth/alert config). No production code was altered in this review.
