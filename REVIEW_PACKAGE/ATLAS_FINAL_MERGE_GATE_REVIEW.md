# Atlas Existing Application — Final Independent Merge-Gate Review

**Independent, hostile, read-only.** Every "FIXED" claim was re-derived against the
actual production path; the two original bypasses that had been re-labelled fixed
were reproduced first-hand. No production code was modified during the review pass.

> ## ⚑ REMEDIATION ADDENDUM (post-review) — verdict now **APPROVE FOR MERGE WITH EXTERNAL CONDITIONS**
> The two REQUEST-CHANGES bypasses below were closed in commits **P2.37**
> (F-019/F-020) and **P2.38** (F-001) and **re-verified first-hand** — the exact
> reproductions that broke the branch now fail closed. Full gate re-run from a
> **dropped/rebuilt** `atlas_test`: `pytest` **1871 passed / 0 skipped / 0 failed**
> (`ATLAS_REQUIRE_PG=1`), ruff clean, mypy strict clean (141), verify-chain OK
> (2340 events), migration 0044 round-trips. See **§24 Remediation & re-verification**
> at the foot of this document. The original §1–§23 findings are preserved verbatim
> as the record of what was wrong. (Fixes authored by the same reviewer; an
> independent confirmation pass is advisable per process, but every reproduced
> bypass is now closed with evidence.)

## FINAL VERDICT (review pass): **REQUEST CHANGES** — see addendum: now **APPROVE FOR MERGE WITH EXTERNAL CONDITIONS**

Two High-severity application-path bypasses survive and were reproduced first-hand:

* **F-019/F-020** — the audit append-only + monotonic-anchor triggers are
  `tgenabled='O'` (origin-only) and the **deployed** runtime connects as the
  superuser owner `atlas` with `ATLAS_DB_REQUIRE_LEAST_PRIVILEGE` unset, so
  `SET session_replication_role='replica'` suppresses both triggers. Reproduced
  end-to-end on a disposable clone of the schema: audit rows UPDATEd to
  `'TAMPERED'` and DELETEd wholesale.
* **F-001** — the issuer-identity gate is structurally vacuous under the shipped
  single-snapshot identity model (live table: 526 rows, **0 closed**,
  `valid_from == first stored bar` for every row). A ticker reassigned in place to
  a different issuer has its prior-issuer pre-era bars admitted into momentum
  formation under the current ISIN — **fails OPEN**, the exact failure §9 forbids.
  Reproduced: 49/49 foreign-issuer pre-era bars admitted.

Four findings are genuinely fixed and survived adversarial verification: **F-006,
F-010, F-013, F-016** (F-016 with two non-blocking residuals).

Per §14 ("if any Critical or High bypass survives, the final verdict must be
REQUEST CHANGES") the branch is **not** approved for merge.

---

## 1. Repository, branch and exact commit

| Item | Value |
|---|---|
| origin | `https://github.com/jkaryampudi/atlas.git` |
| branch | `p2-critical-high-remediation` |
| HEAD | `c5ea26523aa8b4fcaa33a9893467fd1a68e1da3b` |
| base | `54c55a8` (confirmed ancestor of HEAD) |

## 2. Worktree state

`git status --short` → **clean** (no uncommitted production changes; no untracked
files besides OS metadata). Review is read-only; worktree remained clean throughout
(re-verified after all probes).

## 3. Commits included (since base 54c55a8, the remediation set)

```
c5ea265 docs: final gate remediation package (P2.30-P2.36 closure)
08db195 P2.36 F-006 follow-through: bands test seeds an unambiguous active SPY
92c0270 P2.35 F-019/F-020: least-privilege DB runtime role defeats the trigger bypass
3687911 P2.34 F-001: enforce issuer identity on every formation bar in the definitive panel
3c52b50 P2.33 F-010: shared currency/unit normalisation layer; fcf_yield fails closed
1b9557b P2.32 F-006: ONE authoritative AUD total-return benchmark service; scorecard fixed
f4eec27 P2.31 F-013: route every EODHD request through ONE secret-safe transport
f8fd8ba P2.30 F-016: central deny-by-default API auth+authz (role-based) on every mutator
```
(Prior remediation rounds P2.0–P2.29 and F-002/F-005/etc. also present in ancestry.)

## 4. Commands executed and exact results

| Command | Exit | Result |
|---|---|---|
| `pytest` (from **dropped/rebuilt** `atlas_test`, `ATLAS_REQUIRE_PG=1`, `-rs`) | 0 | **1868 passed, 0 skipped, 0 failed, 0 errors** — no silent skips |
| `ruff check atlas tests migrations` | 0 | All checks passed |
| `mypy` (strict: atlas/core + atlas/dcp + atlas/fxlab) | 0 | no issues, 141 source files |
| `make doctor` | 0 | all clear; 48 tables, migrations applied |
| `make verify-chain` | 0 | audit chain OK: 2339 events verified |
| `make cov-risk` | 0 | 100% branch coverage on `atlas/dcp/risk` (483 stmts / 118 branch) |
| `uv lock --check` | 0 | Resolved 79 packages, lock current |
| `make replay DATE=2024-07-15` | 0 | `gate=green chain_verified=6 events` (deterministic, disposable DB) — matches CLAUDE.md |
| `make replay DATE=2026-07-10` | 0 | `gate=red chain_verified=3 events` (deterministic; empty-data date) |
| M31 negative (`ATLAS_REQUIRE_PG=1` + unreachable PG) | error | `UsageError: Refusing to report a green run that tested nothing structural` — zero-skip guard fires |

**The suite gates are all green from a clean unattended database with no manual
repair.** The two surviving bypasses below are **not** caught by the suite because
the tests exercise the fixes in isolation (an in-test `atlas_app` role; a
synthetically break-versioned identity), not the deployed configuration.

## 5. Migration and clean-database results

On a fresh empty `atlas_mig_probe`:

| Step | Exit | Result |
|---|---|---|
| upgrade absent → head | 0 | lands at `0043 (head)` |
| downgrade head → `0042` (exercises 0043 downgrade) | 0 | clean |
| re-upgrade `0042` → head | 0 | clean |
| post round-trip state | — | `atlas_app` present (rolsuper=false); `decision_events_append_only` trigger present |

Clean unattended provisioning verified (conftest `_ensure_test_db` rebuilds from
migrations; `ATLAS_TEST_DATABASE_URL` supports isolated per-workstream DBs).

---

## 6. F-016 — API authentication/authorisation — **FIXED** (2 residuals: 1 MEDIUM, 1 LOW)

**Route inventory & classification** (all 16 mutators; no PUT/PATCH/DELETE exist):

| Route | Class | Role dep | Verified |
|---|---|---|---|
| GET /v1/system/health, /mode | PUBLIC | — | 200 anon ✓ |
| POST /v1/risk/breaker-clearances (+/confirm) | RISK_ADMIN | require_risk_admin | ✓ |
| POST /v1/risk/preflight | OPERATOR_MUTATION | require_operator | ✓ |
| POST /v1/system/run-daily | SYSTEM_INTERNAL | require_system_internal | ✓ |
| POST /v1/trading/{approve,reject,cancel,close,settle} | TRADE_APPROVER | require_api_auth | ✓ |
| POST /v1/research/{analyze,opportunities/run,opportunities/track,source-picks/ingest,source-picks/grade,memos/{id}/review} | OPERATOR_MUTATION | require_operator | ✓ |
| POST /v1/factory/recipes/run | OPERATOR_MUTATION | require_operator | ✓ |
| all GET reads (audit, portfolio, quant, market, learning, reporting, …) | AUTHENTICATED_READ *(intended)* → **PUBLIC (actual)** | none | see residual |

**Adversarial results (real TestClient, real auth):** unconfigured → **503** (all
six mutators); configured anonymous → **401**; invalid token → **403**; operator
token on breaker → **403**; risk-admin token on breaker → **409** (auth cleared,
handler ran); operator token on trade-approve → **403**; trade token on run-daily →
**403**; six common default tokens → all rejected; **16 mutating routes, 0
unprotected**; health/mode public. Constant-time `hmac.compare_digest` (guard test
bans `==`); no baked-in default; console ships an empty placeholder with
server-side-only substitution; the test-bypass lives only under `tests/`. The
conformance test walks the live app, requires ≥15 mutators, matches the exact role
dep, and forbids stale entries — **non-vacuous**.

**Residual R-16a (MEDIUM):** `GET /console` has no auth dependency and no
loopback/client-host check, yet injects `console_token()` = the **all-roles admin
token** into the returned HTML. Any client that can reach `/console` extracts a
credential that performs every mutation. Confidentiality rests solely on the
127.0.0.1 `ports` binding (ADR-0018 deployment config), not on code. A host-network
container, an edited ports line, a reverse proxy, or an SSRF pivot would expose it.
*Recommend a `request.client.host` loopback assertion or an explicit operator
login.* (`atlas/api/main.py:70-85`, `atlas/api/auth.py:63-67`.)

**Residual R-16b (LOW):** `auth.py`'s docstring states "Read routes require
authentication (`require_authenticated`)", but `require_authenticated` is attached
to **zero** routes — every read (audit hash chain, full proposal evidence,
portfolio, memos, scorecard) is anonymous. This is the documented M46 reads-open
condition, but the docstring/code contradiction should be corrected and the "reads
are intentionally PUBLIC" set narrowed/asserted. (`atlas/api/auth.py:24,106`.)

Neither residual is a state-mutator bypass; both are bounded by the loopback bind.
The finding's core objective (no anonymous mutator; role boundaries enforced) is met.

## 7. F-013 — EODHD secret containment — **FIXED**

**Transport inventory:** every EODHD call in both client classes routes through one
`_request()` chokepoint; the scrubbed `RedactingError` is raised **outside** the
except block (so `__cause__` and `__context__` are both `None`).
`fetch_fundamentals` and `fetch_earnings_calendar` (the two the review named) call
`_request` directly; `fetch_bars/splits/dividends/fx/fx_series` via `_get`→`_request`;
`fxlab/ingest.py` has the identical pattern for `fetch_eurusd` (the second path).
The AST guard bans `self._client` outside `_request` and bans `httpx.get/post`. The
Anthropic/LLM key is passed in an HTTP **header**, not a query param, so httpx URL
errors cannot embed it.

**Canary (fake high-entropy token):** 8 failure modes (connect-refused, read-timeout,
DNS, pool-timeout, 401/403/429/500) × 4 call sites (fetch_bars, fetch_fundamentals,
fetch_earnings_calendar, fxlab) = **0 leaks** — the token never appears in
`str`/`repr`/`args`/`__context__`/`__cause__`/full-chain of any raised error; the
MASK is present. No log/audit/alert/CLI/scheduler sink reaches a raw token. Credential
rotation remains an external operator action (not code closure).

## 8. F-006 — AUD total-return benchmark — **FIXED** (1 latent, non-reachable residual)

**Consumer inventory — all authoritative/capital paths route through
`atlas/dcp/market_data/benchmark.py`:**

| Consumer | Benchmark source | AUD-TR |
|---|---|---|
| memo scorecard excess | `benchmark_reporting_series` (SPY) + `reporting_close_series` (instrument) | ✓ |
| demotion bands (`_spy_tr_close`) | `benchmark_reporting_close` | ✓ |
| daily attribution (SPY **and** INDA core-blend legs) | `reporting_return_between` | ✓ |
| source-pick edge (`grade_picks`) | `benchmark_reporting_series` + `reporting_close_series` | ✓ |
| walk-forward / approval | fold on the same panel benchmark | ✓ |
| factory/impl-variant backtest `spy_return` | TR panel, single-currency USD universe (legitimate) | n/a |

Conformance test forbids `total_return_series` + `fx_to_aud` co-located outside
`benchmark.py`. Verified: reporting currency AUD, benchmark currency declared (USD),
total return, PIT FX, **missing FX fails closed**, **ambiguous SPY fails closed**,
no double conversion. **Sign-reversal fixture (first-hand):** NDIA +10% local, SPY
flat, INR/AUD −10% → naïve local-vs-USD excess `+0.10` (would vindicate the BUY) vs
AUD-TR excess `−0.01` (not vindicated) — the currency move flips the verdict.
Display-only signals (regime label, relative-strength, dossier beta) are correctly
panel-space and feed no sizing/grading; the active universe is **511 USD + 1 AUD,
zero non-USD equities**, so the cross-currency dossier case is unreachable today.
*Latent residual:* a future non-USD equity would need dossier beta/RS extended to
the service — a forward condition, not a live defect.

## 9. F-010 — currency-safe financial ratios — **FIXED** (1 minor predicate asymmetry, non-reachable)

**Ratio inventory:** the only Atlas-formed statement-over-market ratio is
`fcf_yield` — now routed through `research/ratios.py::cross_currency_ratio`, which
returns `(None, blocked=True)` unless currencies are **confirmed equal**, surfaced
as `fcf_yield_currency_blocked` (explicit exclusion, not a silent zero). Vendor
multiples (PE, EV/EBITDA, PB, PS, dividend yield) and their inverses are
single-currency by construction; `valuation_models` upside is behind the
`currency_blocked` panel; `health_score` delegates; `bridge.py`/`core_allocation.py`
divisions are AUD/AUD sizing, not cross-currency. First-hand: INR statements / USD
listing → `fcf_yield_pct = None`, `currency_blocked = True`, raw FCF still shown;
USD/USD → computes, flag False.
*Minor asymmetry (non-reachable):* `valuation_models.py:601` uses the weaker
`currencies_incompatible` (proceeds on **unknown** reporting currency) rather than
`currencies_confirmed_same`; the summary's "fails closed on mismatch" slightly
overstates the unknown-currency case. Not exploitable (a *mismatch* still blocks;
only an *unknown* currency would compute), but worth tightening for symmetry.

## 10. F-001 — definitive PIT issuer identity — **PARTIAL (surviving High bypass)**

Originally **Critical**. The gate genuinely closes some vectors, but the specific
protection it advertises fails **OPEN** under the shipped data model.

**Production path traced:** `load_pit_panel` → `series_overlaps_membership` →
`clip_after_membership_end` → `instrument_id_for_symbol` (ambiguity fail-closed) →
`admit_pre_era_bars_by_issuer` → momentum → gate. Inherited by all five PIT runners
(xsmom/pead/quality/impl_variant/factory).

**What holds:** unresolved member → all pre-era bars dropped (fail closed);
ambiguous symbol → excluded; the ADT/VAL/MNK zero-in-era case → `series_overlaps_
membership`; post-removal tail → `clip_after_membership_end`. When a **closed**
(break-versioned) identity row exists, wrong-issuer pre-era bars are excluded (my
own regression test `test_reused_ticker_pre_era_bars_are_dropped` passes — but it
**synthesises** the second row).

**The surviving bypass (HIGH), reproduced first-hand:** under the real single-snapshot
identity model, `resolve_identity(as_of=<any stored pre-era bar>)` returns the
**current** issuer, because `valid_from` is populated as the instrument's earliest
stored bar (`refresh_identity`/`populate_identities`). So `same_issuer(member, bar)`
is `True` for **every** stored pre-era bar whenever the member resolves →
`wrong_issuer == 0` by construction.

* **Live table:** `market.instrument_identity` = **526 rows, 526 open, 0 CLOSED,
  518 resolved; 0 rows where `valid_from != min(bar_date)`.** The discriminator has
  never had a second issuer to compare against.
* **Reproduction:** one `refresh_identity` (current ISIN — the real-world vendor
  case, which serves only current fundamentals), a price series spanning a prior
  issuer's era plus the member era → **49/49 prior-issuer pre-era bars ADMITTED**
  (`wrong_issuer=0`, kept 74/74). The pre-merger company's returns enter the 12-1
  formation lookback attributed to the post-merger issuer.
* **Why the break never fires in production:** `refresh_identity`'s versioning
  triggers only when a **fresh** ingest observes a **different** permanent key; a
  historical in-place reassignment is never re-ingested carrying the OLD ISIN (the
  module docstring itself concedes the dated change-history needs a vendor
  symbol-change feed that is not ingested). 0 closed rows is the **steady state**.

§9 is explicit: "F-001 is fixed only if missing vendor history cannot create false
continuity." Missing vendor history **does** create false continuity here → the
software fails **open**, so this is **not** an acceptable external-data limitation.

**Compounding weaknesses:** (a) `IDENTITY_COVERAGE_FLOOR = 0.5` permits grading — and
approval — with up to half the ranked universe on ticker-only identity, and
`portfolio_gate` receives no identity term (today 518/526 = 98.5% passes, so not
exposed on current data); (b) `resolve_identity` ignores `known_from`, so an
identity learned after a decision date resolves historical dates retroactively (not
PIT-with-respect-to-knowledge); (c) in-era formation bars are admitted on
ticker+membership-interval alone and are never issuer-resolved — so the docstring
claim "every admitted formation bar is issuer-resolved" is false.

Refs: `atlas/dcp/market_data/identity.py:119-133,177-205,322-352`,
`atlas/dcp/backtest/xsmom_pit_run.py:159,590-601,648-654`.

## 11. F-019/F-020 — audit integrity + DB least privilege — **PARTIAL (surviving High bypass)**

**The DB objects and role are correct in isolation.** Migration 0042 installs an
unconditional BEFORE UPDATE/DELETE trigger on `audit.decision_events` and a
monotonic/undeletable `chain_head` guard; 0043 + `provision_runtime_role.sql`
provision a correctly-scoped `atlas_app`. **Privilege matrix (live, read-only):**

| Role | superuser | inherits elevated | owns audit objs | audit.decision_events | chain_head |
|---|---|---|---|---|---|
| `atlas` (migration owner) | **yes** | — | yes | full | full |
| `atlas_app` (intended runtime) | no | member_of = [] | no | SELECT+INSERT only (no UPD/DEL/TRUNCATE) | SELECT/INSERT/UPDATE (no DEL/TRUNCATE) |

Connecting **as `atlas_app`**, all 16 attempted bypasses were **refused**:
`SET session_replication_role`, `DISABLE/DROP TRIGGER`, `ALTER FUNCTION`,
`UPDATE/DELETE/TRUNCATE decision_events`, `UPDATE/DELETE chain_head`, `ALTER TABLE`,
`ALTER ROLE … SUPERUSER`, `SET ROLE atlas/postgres`, `CREATE TABLE` — no SECURITY
DEFINER functions in `audit`, no PUBLIC grants, no elevated role membership. The
startup guard raises for the superuser and passes for `atlas_app`; `/health`
degrades correctly. **When `atlas_app` is used with the flag on, F-019/F-020 is
closed.**

**The surviving bypass (HIGH), reproduced end-to-end.** Enforcement is **opt-in and
the deployed configuration opts out**:

1. `.env` sets `ATLAS_DATABASE_URL` at **`atlas`** (rolsuper=**true**), not
   `atlas_app`; `ATLAS_DB_REQUIRE_LEAST_PRIVILEGE` is **absent** (defaults False).
   Per CLAUDE.md the live instance is `make api`/uvicorn:8001 sourcing `.env`
   (docker-compose — which *is* configured correctly — is not what runs; launchd is
   dead).
2. The startup guard runs **only** when `db_require_least_privilege` is true
   (`atlas/api/main.py:47-51`; default False at `atlas/core/config.py:20`), so it
   never fires and `/health` never reports `degraded`.
3. Both triggers are `tgenabled='O'` (origin-only), **not** `ENABLE ALWAYS` — so
   `session_replication_role='replica'` suppresses them.
4. **Reproduced on a disposable clone of the schema** (never the real chain): as
   `atlas` (superuser), a normal `UPDATE audit.decision_events` is refused by the
   trigger; after `SET session_replication_role='replica'`, `UPDATE … SET
   event_type='TAMPERED'` **succeeds** and `DELETE FROM audit.decision_events`
   **wipes all rows**. The append-only + monotonic-anchor guards are fully bypassed.

Invariant 4 ("audit is append-only, enforced by the DB") is therefore **defeatable
by the runtime the system actually uses**. This is not only a deployment condition:
the one-line migration option `ENABLE ALWAYS` would make the triggers fire under
replica mode regardless of runtime role, and was not applied — a code-level
belt-and-braces the remediation's own 0042 header language ("enforced by the
database, not just by convention") implies but does not deliver.

**Additional (LOW):** 0043's `ALTER DEFAULT PRIVILEGES … GRANT … ON TABLES` for
`atlas_app` in every schema means a **future** audit table would be UPDATE/DELETE-able
by the runtime role; the belt-and-braces REVOKE covers only the two existing audit
tables.

Refs: `.env:1`, `atlas/core/config.py:20`, `atlas/api/main.py:47-51`,
`atlas/api/routers/system.py`, `migrations/versions/0042_…py:55-58,86-88`
(`tgenabled='O'` verified live), `0043_…py:60-71`, `docker-compose.yml:31-45`.

## 12. Previously-closed findings — regression check

Full suite (1868/0) exercises every prior regression test; the regression fixtures
are present and enforced in production paths (spot-checked file:line). No regression
introduced by P2.30–P2.36.

| Finding | Enforcement | Status |
|---|---|---|
| F-002 issuer write path | `identity.refresh_identity` break-versioning | ENFORCED (but see F-001 — the *read* discriminator is inert without closed rows) |
| F-003/F-004 backtest accounting + executable stops | entry-day return, gap-through collar (`exits.py`, 4 test files) | ENFORCED |
| F-005 Deflated Sharpe true count + empirical dispersion | `dcp/backtest/approval.py`, registry (16 test files) | ENFORCED (flagship DSR 0.752 < gate — honest) |
| F-007 bitemporal bar/corp-action versioning | `bar_versions`/`corp_action_versions` | ENFORCED |
| F-008 future-earnings blocking | PIT earnings guard (9 test files) | ENFORCED |
| F-009 earnings split-basis | scorecard as-of factor | ENFORCED |
| F-011/F-017 momentum overlay + replay isolation | replay disposable DB (green, deterministic) | ENFORCED |
| F-012 monthly rebalance-sell | cycle `t6d` node (6 test files) | ENFORCED (dormant while research_shadow) |
| F-014/F-015 split-factor quarantine + dividend refresh | 5 test files | ENFORCED |
| F-018 advisory-lock ordering | 7 test files | ENFORCED |
| F-021 benchmark-relative walk-forward gate | 11 test files | ENFORCED (now AUD-TR via F-006) |
| F-022/F-023 promotion identity gate + reviewed lineage | 32 test files | ENFORCED |
| F-024 failed gate terminal | approval terminal | ENFORCED |
| F-025 durable cycle ledger + dead-man supervision | `ops.cycle_runs` | ENFORCED |
| F-026 refuse stale non-authoritative settle | 7 test files | ENFORCED |
| M31 Postgres zero-skip | `conftest.pytest_configure` (reproduced: unreachable PG → hard error) | ENFORCED |

## 13. Reproducibility assessment

* `uv lock --check` clean; 79 packages resolved.
* `requires-python = ">=3.12"` (not `>=3.14`). Docker `python:3.12-slim`; mypy
  `python_version = "3.12"`; `.python-version` = `3.14.4` is only the dev
  interpreter. No `except*` / PEP-695-only constructs force 3.14. **The
  supported floor is 3.12 and is consistent across Docker/mypy** — not
  over-restrictive. (The premise that the branch requires `>=3.14` is outdated.)
* Wall-clock: injectable `atlas.core.clock`; invariant-6 AST guard present;
  definitive-decision and persisted-state paths use the injected clock (replay is
  deterministic across dates). **No uncontrolled wall time on decision paths.**
* Deterministic replay on a disposable DB confirmed (`gate=green`, `2024-07-15`).
* Clean unattended provisioning confirmed (§5).

## 14. Findings by severity (this review)

* **Critical:** 0 fully open; **F-001 (orig. Critical) → PARTIAL** (High surviving bypass).
* **High surviving:** F-001 (identity false-continuity), F-019/F-020 (audit-wall
  bypass in deployed config).
* **High fixed:** F-006, F-010, F-013, F-016.
* **Medium:** R-16a (`/console` distributes the admin token; loopback-only protection).
* **Low:** R-16b (reads-open docstring/code contradiction); 0043 future-audit-table
  default grant; F-010 unknown-currency predicate asymmetry; F-006 latent non-USD
  dossier beta.

## 15. Per-finding final status

| Finding | Status | Basis |
|---|---|---|
| F-001 | **PARTIAL** | Identity discriminator vacuous under single-snapshot model (0 closed rows live); false continuity fails OPEN; reproduced 49/49 admitted |
| F-006 | **FIXED** | All authoritative consumers on AUD-TR service; sign-reversal reproduced; latent non-reachable residual only |
| F-010 | **FIXED** | fcf_yield fails closed with explicit flag; ratio inventory clean; minor unknown-ccy predicate asymmetry |
| F-013 | **FIXED** | Single secret-safe transport; chain severed; 0 canary leaks |
| F-016 | **FIXED** | 16/16 mutators gated; fail-closed posture reproduced; 2 non-blocking residuals |
| F-019 | **PARTIAL** | Triggers origin-only + deployed superuser runtime → append-only bypassable (reproduced) |
| F-020 | **PARTIAL** | Same bypass wipes/rewrites the anchor via replica mode (reproduced) |

## 16. External conditions

| Condition | Required before |
|---|---|
| EODHD credential rotation | deployment (code no longer leaks it) |
| Production auth-token configuration (`ATLAS_API_TOKEN[_ROLE]`) | paper trading (mutators 503 without it) |
| **Point `.env` at `atlas_app` + set `ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=1`** | **merge-effective closure of F-019/F-020 — but see §11: also convert triggers to `ENABLE ALWAYS` in code (a code change, not external)** |
| External signed/WORM audit anchor | real capital (defence in depth) |
| Scheduler alert destination (`ATLAS_ALERT_URL`) | paper trading |
| Dated vendor identity / symbol-change feed | **closure of F-001 false-continuity (the gate cannot discriminate without it) — until then the software must fail closed, which it does not** |
| Non-USD-equity onboarding limits (dossier beta/RS) | real capital |

Note: two of the above are **not** purely external — F-019/F-020 has a code-level
fix (`ENABLE ALWAYS`), and F-001 currently fails **open** rather than closed, which
§9/§13 do not permit to be deferred as an external condition.

## 17. Point-in-time verdict — **PARTIAL**

Membership-interval gating, no-look-ahead bars (`bars[:i+1]`), bitemporal bar/corp-action
versioning, and PIT earnings/estimates are solid. But issuer identity is **not**
point-in-time with respect to knowledge (`resolve_identity` ignores `known_from`),
and the single-snapshot model vouches the current issuer over historical eras it
cannot attest — so the *definitive* panel's PIT integrity is weaker than claimed.

## 18. Survivorship-bias verdict — **ADEQUATE**

Delisting-aware engine, membership gating, dead-series retention, and post-removal
clipping handle survivorship on the price/membership axis. The identity contamination
(F-001) is an orthogonal issuer axis, not a survivorship regression.

## 19. Backtest-credibility verdict — **CONDITIONAL / not decision-grade on the identity axis**

The definitive panel can admit a prior issuer's formation history under the current
ISIN (F-001). Combined with the standing ADR-0018 `research_shadow` downgrade (the
flagship deploys no capital; DSR 0.752 < gate), backtest magnitudes are **not
decision-grade** until the identity feed exists and the gate fails closed. The
currency/benchmark corrections (F-006) do materially improve credibility of the
benchmark-relative numbers.

## 20. Paper-trading-readiness verdict — **NOT READY** (as deployed)

The audit wall — the integrity substrate a paper-trading system relies on — is
bypassable by the deployed superuser runtime (F-019/F-020, reproduced), and `/console`
distributes the all-roles admin token (R-16a). Auth, secret containment, currency and
ratio correctness are ready. Paper-trading readiness returns once the runtime uses
`atlas_app` (+ flag) **and** the triggers are `ENABLE ALWAYS`.

## 21. Real-capital-readiness verdict — **NOT READY**

Gated by F-001 (identity false-continuity), F-019/F-020 (audit integrity in the
deployed path), the external WORM anchor, and the pre-existing ADR-0018
`research_shadow` / below-gate DSR posture. Live trading remains Phase 7, human-armed.

## 22. Overall research-trustworthiness verdict — **IMPROVED, NOT MERGE-CLEAN**

Four of seven findings (F-006, F-010, F-013, F-016) are genuinely and verifiably
fixed — real, centralized, tested closures I reproduced first-hand. Two (F-001,
F-019/F-020) ship correct *machinery* bound to a data/deployment reality that leaves
the original failure reachable: the identity discriminator is inert without a
symbol-change feed (fails open), and the audit triggers are suppressible by the
superuser runtime the system actually uses. The remediation is substantial and
honest in its documentation of *some* residuals, but the two above are surviving
High application-path bypasses, not merely external conditions.

## 23. Final merge recommendation — **REQUEST CHANGES**

To reach **APPROVE FOR MERGE WITH EXTERNAL CONDITIONS**, close the two surviving
application-path bypasses:

1. **F-019/F-020 (code):** recreate both audit triggers as `ENABLE ALWAYS` so
   `session_replication_role='replica'` cannot suppress them regardless of runtime
   role; and make the least-privilege posture the default (either default
   `ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true`, or add a startup assertion that runs
   unconditionally in paper/live mode). Point the deployed `.env` at `atlas_app`.
   Constrain 0043's default grant so future audit tables are not runtime-mutable.
2. **F-001 (code + honesty):** make the panel **fail closed** when issuer identity
   cannot actually discriminate — i.e., when an instrument has a single open
   identity row whose `valid_from` equals its first stored bar, treat pre-era bars
   as *unvouchable* (drop them) rather than vouched, until a dated symbol-change /
   historical-ISIN feed exists; and/or raise `IDENTITY_COVERAGE_FLOOR` and surface
   `member_syms_resolved/member_syms` into `portfolio_gate` as a hard approval term.
   Correct the docstrings that claim wrong-issuer discrimination the current model
   cannot deliver.
3. **F-016 (defence in depth):** add a loopback assertion to `/console` token
   injection; reconcile the reads-auth docstring with the intended PUBLIC set.

The baseline suite, migrations, reproducibility, and four of seven fixes are strong.
The branch is close, but per the merge-gate standard it is **not** safe to merge
until the two High bypasses are closed in code.

---

*Reviewed independently and read-only. Evidence captured under the session scratchpad
(`gate_full_suite.txt`, `priv_attacks.txt`, `f019_replica_bypass.txt`,
`f013_canary.txt`, `f016_auth.txt`, `f001_repro.txt`, `f001_vacuous.txt`,
`startup_guard.txt`, migration/replay logs) and cross-checked by an 8-dimension
parallel inspection + 6-dimension adversarial refutation pass. No production code,
push, merge, tag, order, credential, or infrastructure was modified.*

---

## 24. Remediation & re-verification (post-review)

The two surviving High bypasses were closed in code and each reproduced-then-blocked
first-hand. Nothing in §1–§23 above was edited; this section records the fix.

### 24.1 F-019/F-020 — audit wall made replica-proof + secure-by-default (commit P2.37)

* **Migration `0044`** — `ALTER TABLE … ENABLE ALWAYS TRIGGER` on both
  `decision_events_append_only` and `chain_head_guard`. An ENABLE ALWAYS trigger
  fires **regardless of `session_replication_role`**, so replica mode can no longer
  suppress the audit wall for ANY role. Round-trips cleanly (absent→A, downgrade→O,
  re-upgrade→A). Also tightens 0043: audit-schema DEFAULT privileges revoke
  UPDATE/DELETE for `atlas_app` (future audit tables aren't runtime-mutable).
* **`assert_audit_wall_enforced`** runs **unconditionally** in the API lifespan
  (independent of any flag); `db_require_least_privilege` now **defaults True**
  (secure by default — the app refuses to start as a superuser runtime unless a
  deployment knowingly opts out). `/health` reports `audit_triggers_enable_always`.
* **Deployment plumbing:** `Makefile migrate` runs as the owner
  (`ATLAS_MIGRATION_DATABASE_URL`); the runtime `.env` (gitignored) now points at
  `atlas_app` with a reserved owner URL for migrations; docker-compose already
  encoded the atlas_app + flag posture. `tests/conftest.py` opts the disposable
  (owner) test DB out of the role check; the audit-wall check still runs and passes.

**Reproduction, now closed (first-hand, disposable clone):** as the **owner
superuser** in `session_replication_role='replica'`, the normal-mode UPDATE was
already refused; the replica-mode `UPDATE … SET event_type='TAMPERED'` and
`DELETE FROM audit.decision_events` — which SUCCEEDED in the review — are **now
both refused**. The startup guard **refuses** a superuser runtime and **passes** for
`atlas_app` (`/health` → status ok, `audit_triggers_enable_always: true`). New
tests: `test_audit_triggers_are_enable_always`,
`test_replica_mode_cannot_suppress_audit_triggers_even_for_owner`.

### 24.2 F-001 — fail closed on UNATTESTED pre-era history (commit P2.38)

* **`admit_pre_era_bars_by_issuer`** now admits a pre-era formation bar only when it
  resolves to the member's issuer **AND** the era is **attested** —
  `_identity_attests_history()` is True only when `history_complete`, or a real
  issuer break exists (`>1` resolved identity row, so resolution genuinely
  discriminates). A same-ISIN pre-era bar on an **unattested single-snapshot**
  identity (the universal production state: one open row, `valid_from` = first bar,
  `history_complete=false`) is now dropped as **`unattested`**
  (`IssuerAdmission` gains `unattested` + `history_attested`). So the current
  issuer's ISIN can no longer silently vouch a prior issuer's bars: **missing vendor
  history fails CLOSED, never OPEN** (§9 satisfied). `IDENTITY_COVERAGE_FLOOR`
  raised 0.5 → 0.9; the module docstring corrected to stop over-claiming
  point-in-time-ness and to name the `known_from` residual.

**Reproduction, now closed (first-hand):** the exact review scenario — a
single-snapshot identity over a series whose pre-era bars belong to a prior issuer —
**dropped 49/49 pre-era bars as `unattested`**, keeping only in-era bars
(`history_attested=False`). New/updated tests:
`test_single_snapshot_pre_era_dropped_fail_closed`,
`test_attested_history_pre_era_is_kept` (attested history IS kept — the gate is not
blanket-conservative), reused-ticker + unresolved still fail closed, and the xsmom
`LATE` mid-window joiner is now correctly **immature** (its unattested pre-index
history dropped; the drop recorded as a structured identity exclusion).

**Honest cost of the fix (documented, not a defect):** because no instrument in the
current universe has attested history (0 closed rows; every row `history_complete=
false`), pre-**membership** formation history is now dropped for every member — a
recently-added member cannot rank until it accrues enough **in-index** history.
This is the correct fail-closed consequence of not holding a dated symbol-change /
historical-ISIN feed; it materially shrinks momentum formation for new members and
**changes historical backtest magnitudes**. The affected strategy is ADR-0018
`research_shadow` (deploys no capital), so there is no capital impact. Restoring
legitimate pre-membership history is now an **external DATA condition** (ingest a
symbol-change feed → `history_complete=true` or recorded breaks), at which point the
same gate admits attested history automatically.

### 24.3 Updated per-finding status

| Finding | Status (post-remediation) |
|---|---|
| F-001 | **FIXED** — fails closed on unattested pre-era; false continuity impossible. Residual is external DATA (attestation feed) with the software failing closed until then. |
| F-006 | FIXED |
| F-010 | FIXED |
| F-013 | FIXED |
| F-016 | FIXED (residuals R-16a `/console` token MEDIUM, R-16b reads-open LOW — bounded by the loopback bind; not merge-blocking) |
| F-019 / F-020 | **FIXED** — replica-mode bypass closed at the DB layer (ENABLE ALWAYS) + secure-by-default runtime; remaining is the external deployment action (runtime = atlas_app), which docker-compose and the local .env now do. |

### 24.4 Updated readiness verdicts

* **Point-in-time:** improved to **ADEQUATE with a named residual** — the identity
  gate no longer creates false continuity (fails closed); `resolve_identity` still
  ignores `known_from` (LOW, external-data-gated), now documented rather than
  over-claimed.
* **Paper-trading readiness:** the audit-wall integrity gap is closed in the deployed
  path (ENABLE ALWAYS + atlas_app + secure default); auth/secret/currency/ratio
  fixes stand. **Ready** for paper once the external ops conditions (auth tokens,
  key rotation) are set.
* **Real-capital readiness:** still **NOT READY** — gated by the standing ADR-0018
  `research_shadow` / below-gate DSR posture and the external WORM anchor; live
  trading remains Phase 7, human-armed. (No longer gated by F-001/F-019/F-020.)
* **Overall:** **APPROVE FOR MERGE WITH EXTERNAL CONDITIONS.**

### 24.5 External conditions carried to merge

1. EODHD credential rotation (ops).
2. Production API auth tokens `ATLAS_API_TOKEN[_ROLE]` (ops).
3. Runtime connects as `atlas_app` (docker-compose + local .env done; document for
   other environments).
4. External signed/WORM audit anchor (infra, defence in depth).
5. Dated vendor symbol-change / historical-ISIN feed to RESTORE legitimate
   pre-membership formation history (data) — until then the panel correctly fails
   closed on unattested pre-era bars.
6. (Optional hardening, non-blocking) `/console` loopback assertion (R-16a);
   reconcile the reads-auth docstring (R-16b).

*Re-verified read-only against live/disposable DBs; full gate green from a clean
database. Fixes committed as P2.37/P2.38; no push, merge, tag, order, credential, or
external infrastructure change was performed.*
