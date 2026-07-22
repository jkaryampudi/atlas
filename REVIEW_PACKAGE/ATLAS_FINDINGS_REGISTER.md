# Atlas — Existing Application Findings Register

**Review date:** 2026-07-21 · **Commit:** `d65e0b1` (branch `p1-strategy-artifact`; production code byte-identical to `main`/`p0-research-shadow` @ `54c55a8`) · **Method:** 18 parallel area reviewers + 25 adversarial scenarios + adversarial verification of every critical/high finding + 6 direct DB-mutation probes on a disposable `atlas_test`.

**Verification:** every High/Critical below was re-checked by an independent agent instructed to *refute* it. Of 36 raw High findings, **0 were refuted**; 1 was lifted to Critical; several were merged (the panel independently reported the same defect from multiple angles). Findings are de-duplicated here. `[V]` = adversarially verified & confirmed; `[V→C]` = verified and severity-raised; `[P]` = probed directly by the reviewer.

**Severity counts (de-duplicated distinct findings):** Critical **1** · High **26** · Medium **~50** · Low **~40** · Informational **~27**.

---

## CRITICAL

| ID | Area | Finding | Evidence | Impact | Recommendation | Test required |
|----|------|---------|----------|--------|----------------|---------------|
| **F-001** `[V→C]` | PIT / Security-master | **Wrong-issuer / wrong-era data spliced into the "definitive" PIT validation panel.** The `pit-sp500` universe joins interval membership to a price series keyed on `symbol`, with one membership spell per ticker and no issuer identity. Names appear in the panel whose bars are entirely *outside* their membership era, or belong to a *different company* that later reused the ticker. | Live dev DB: 3 members whose series starts *after* their membership `end_date` (ADT, VAL, MNK — zero member-era bars) and 24 delisted members with bars >30d past `end_date`; `index_membership.py:22-26` documents the ALTR ticker-reuse confusion; `migrations/0015:33-43` PK = one spell/ticker. | Contaminates the total-return magnitudes, DSR inputs and benchmark comparison that back the flagship's validated claims — a source of the ADR-0018 "not reproducible / not trustworthy" verdict, quantified. | Add issuer-level identity (FIGI); represent multiple membership spells; refuse panel rows outside the member era; re-derive validation magnitudes. | PG test: a reused/renamed ticker cannot contribute out-of-era bars to a PIT panel. |

---

## HIGH

| ID | Area | Finding | Evidence | Impact | Recommendation | Test required |
|----|------|---------|----------|--------|----------------|---------------|
| **F-002** `[V]` | Security-master (area verdict **FAIL**) | **No issuer-level identity anywhere; `symbol`+`exchange` is the only key.** A reused ticker splices a different company's bars onto the old UUID with zero detection; `symbol_change` exists only as a CHECK value with no writer/reader/rows. | `migrations/0001:22-35,49-55`; `ingest.py:128-140` (symbol-keyed fetch→UUID); grep `isin\|cusip\|figi` → 0 hits. | Historical data can attach to the wrong security; breaks survivorship & PIT correctness at the identity root. | Add FIGI/ISIN column; implement `symbol_change`; re-listing detector refusing bar appends across an identity break. | Ticker-reuse splice test; symbol-change remap test. |
| **F-003** `[V]` | Backtest engine | **Single-instrument engine double-counts the entry-day return.** `engine.py:96` uses `>` on the exit path vs `>=` on the hold path (`:100`); on a next-day exit the entry-day gain is booked twice. Reproduced with the real `run_backtest`. | `atlas/dcp/backtest/engine.py:96,100`. | Inflates reported returns of every single-symbol backtest (momentum v1, trend/meanrev/breakout candidates, validation lane). | Unify to `>=`; re-pin goldens; re-run affected trials. | Hand-calc golden: entry→next-day exit books the return exactly once. |
| **F-004** `[V]` | Backtest engine | **Impossible/optimistic stop fills.** `engine.py:86-87` fills any `b.low <= stop` exit *at the stop price* with no gap clamp; a gap-through-stop fills at an unobtainable price. (Production `exits.py` is correct — `min(stop, open)` — so backtest is *more* optimistic than live.) | `engine.py:86-87`; contrast `exits.py:72-78,289`. | Systematically understates drawdowns/losses in backtests; live will underperform the backtest on gaps. | `stop_price = costs.sell(min(stop, b.open))`; re-pin goldens. | Gap-down-through-stop golden vs hand calc. |
| **F-005** `[V]` | Performance metrics | **Deflated Sharpe substitutes the minimum-possible trial variance (1/T)** for the true cross-trial Sharpe variance in the BLdP expected-max term; no PSR denominator. Overstates DSR. | `atlas/dcp/backtest/validation.py:17-30` (`V[SR]=1/T` at :26). | The headline "DSR 0.995 / 0.90 gate" overstates statistical support; compounds the already-below-gate honest DSR (≈0.85). | Implement PSR/DSR with the empirical variance of trial Sharpes across the lineage. | Unit test vs a worked BLdP example. |
| **F-006** `[V]` | Performance metrics | **Currency-mismatched alpha:** AUD sleeve returns graded against a USD SPY total-return benchmark. | `bands.py:269-276`, `attribution.py:274-280` (AUD marks) vs `_spy_tr_close` (USD). | Every alpha/excess figure is polluted by USD/AUD FX drift; the approval bar (ADR-0009 "beat SPY TR") is measured in mixed currency. | Convert both legs to one currency before differencing; restate exhibits. | Test: identical-return series in two currencies → alpha≈FX drift flagged. |
| **F-007** `[V,P]` | Market data / PIT / Repro | **In-place bar overwrite destroys history & knowledge-time.** `upsert_bar` `ON CONFLICT DO UPDATE` rewrites OHLCV and does *not* refresh `ingested_at`/`quality_flags`; no raw-payload retention; corporate actions are conversely first-write-wins (vendor corrections ignored forever). | `ingest.py:40-49,56-78`; probe P7: owner UPDATE of a historical close 100→250 succeeded, undetected. | A provider revision silently rewrites what past decisions saw → past research is unreconstructable & unverifiable (the ADR-0018 non-reproducibility root). | Bitemporal/versioned bars or a `market.bar.revised` audit event on content change; retain raw payloads. | Revision-detection test; PG immutability test on bars. |
| **F-008** `[V]` | Fundamentals / PIT | **Vendor future-dated earnings "actuals" stored as immutable facts; `reportDate` trusted blindly.** 67 rows with `report_date > fetched_at`; PVH `report_date 2026-08-25 > fetched_at 2026-07-15` with a concrete `eps_actual`. No `report_date>fiscal_period_end` or `<=fetch` check. | `earnings_history.py:121-160`; migration 0021 (no date CHECK). | A fabricated "actual" before it could be known is a direct look-ahead into any PEAD/surprise consumer. | Ingest guard: `fiscal_period_end < report_date <= fetch_date`; drop/flag others. | PG test: future-dated actual is refused. |
| **F-009** `[V]` | Fundamentals | **Mixed split-basis in `earnings_surprises`.** EPS stored on the vendor's *current* share basis under append-only `DO NOTHING`; the first post-ingest split silently mixes bases within one instrument's series. | `earnings_history.py:27-32,139-160`. | Time-bomb correctness defect in an "immutable facts" table; corrupts SUE/PEAD signals after any split. | Store split-adjustment basis + as-of; re-derive on split; or version on `fetched_at`. | Split-after-ingest basis-consistency test. |
| **F-010** `[V]` | Fundamentals / Factors | **Cross-currency field mixing for ADRs.** EODHD statement-currency `Financials.*` / `RevenueTTM` mixed with USD `MarketCapitalization`/price in ratios. | `health_score.py:104,114`; `valuation_models.py`. | Corrupts research health-score and valuation-model outputs for every non-USD-reporting ADR. | Normalise all legs to one currency using statement currency + FX before ratio. | Cross-currency ratio test (a non-USD reporter). |
| **F-011** `[V]` | Risk / Selection | **Dead `MUTANT_no_such_state` sentinel disables the §12 momentum-factor overlay.** Both SQL filters use `st.state IN ('MUTANT_no_such_state')` (structurally unmatchable — 0035 CHECK enumerates 9 real states); a surviving mutation-test artifact from commit `9408a20`. | `proposals.py:589,621`; `migrations/0035:45-47`. | A documented risk control (§12 momentum attribution) never runs; fails safe (over-refuses) but the control is decorative. | Restore `('paper','live')`; add a mutation-survivor test. | Test: overlay actually fires on a paper/live momentum name. |
| **F-012** `[V]` | Selection | **Deployed sleeve has no sell-side monthly rebalance.** The T0-T9 node list has no rebalance-sell node; the only sells are ATR stops + human close. The validated construct rebalances monthly. | `daily.py:657-676`; `exits.py:197,347`. | Deployed behaviour structurally diverges from the validated strategy — evidence accrues on a different construct. | Add a monthly rebalance-sell node matching the validated cadence, or re-validate the buy-and-hold-with-stops variant. | Cycle test asserting monthly turnover on the sleeve. |
| **F-013** `[V]` | Security | **Live EODHD API key leaked into the append-only audit hash chain and logs.** The key travels as a URL query param (`api_token`); `raise_for_status()` embeds the full URL (incl. key) in `httpx` exceptions that reach audit payloads / logs. | `eodhd.py:90-95,154-159,182`. | A production secret is persisted in an immutable, exported artifact and stderr; unredactable after the fact. | Move the token to a header; scrub URLs in error handling; rotate the key. | Test: an adapter error never surfaces the token. |
| **F-014** `[V]` | Market data | **Vendor "splits" feed carries non-split factors applied blindly as price adjustments.** | `ingest.py` splits path; `adjustment.py`. | Spurious price adjustments corrupt the derived split-adjusted & total-return series feeding signals. | Validate split factors (ratio sanity, effective-date); quarantine anomalies. | Split-factor sanity test. |
| **F-015** `[V]` | Market data / PIT | **Dividends are never refreshed by the nightly cycle.** `run_daily_ingest` fetches splits+bars+FX+fundamentals+earnings+estimates but never `fetch_dividends`; the dividend store is already 7 days behind bars. | `daily.py:361-402`. | Total-return series and PEAD event store silently decay — the exact data class whose absence drove the ADR-0018 downgrade. | Add a dividend refresh node to the daily cycle; alert on staleness. | Cycle test asserting dividend store freshness. |
| **F-016** `[V]` | Security | **Zero authentication on every API endpoint, incl. trade approval.** No `Depends`/`Security`/`HTTPBearer`/`APIKeyHeader` anywhere in `atlas/api/`; the approve endpoint's only "gate" is a caller-supplied `acknowledged_risks` boolean; auth "deferred" per its own docstring. Only control is the loopback bind. | `atlas/api/` (0 auth deps, verified); `trading.py:144-164`. | Any local process (or a DNS-rebinding browser page, F-Med) can approve proposals, cancel orders, close positions. Blocks real-capital readiness. | Add authn + step-up on state-mutating endpoints; verify approver identity; Host-header/CORS hardening. | Endpoint-auth test; approve without token → 401. |
| **F-017** `[V,P]` | Reproducibility | **Documented `make replay` writes fixtures into the configured (prod) DB.** `replay.py` runs `seed_instruments` + `ingest_day(FixtureAdapter)` through `session_scope()`; the Makefile target loads `.env` (prod `atlas`). No test-DB guard. | `Makefile:17-18`; `replay.py:22-45`. | Running the documented determinism command contaminates/overwrites real market data via `DO UPDATE`. | Guard replay to a disposable DB (refuse non-`*_test`); or use an isolated schema. | Replay refuses a non-test DSN. |
| **F-018** `[V,P]` | Database | **ABBA advisory-lock inversion → deadlock.** The daily cycle acquires the audit lock (`762001`) then wants the trading-lifecycle lock (`hashtext('atlas.trading.lifecycle')`); a concurrent API approve holds the lifecycle lock then appends audit (`762001`). | `audit_repo.py:18,29`; `proposals.py:218`. | A human approval during the 23:30 cycle can deadlock; Postgres aborts one txn — potentially rolling back the atomic daily cycle. | Impose a global lock-acquisition order; document & test it. | Concurrency test forcing the ABBA order. |
| **F-019** `[V,P]` | Database / Audit | **Audit chain hash omits `entity_type`, `entity_id`, `actor_type`, `actor_id`.** `link_hash` covers only `prev_hash\|payload_hash\|event_type\|created_at`; those four columns are physically mutable and unprotected by verification. | `audit.py:34-36,95-115`. | An attacker/operator can rewrite who/what an event referenced without breaking the chain. | Fold entity/actor columns into `link_hash` (new chain epoch). | Tamper test on `entity_id`/`actor_id` detected. |
| **F-020** `[V,P]` | Audit / Test | **Audit-chain tail truncation is undetectable and untested.** `verify_chain` walks forward from GENESIS with no tail anchor; deleting the last N rows leaves a valid shorter chain. Probe P5 confirmed: after tail delete, `verify_chain` returns OK. Interior deletion *is* caught (P6). | `audit.py:95-115`; probes P4-P6. | The most safety-critical log can be silently truncated (recent approvals/orders erased). | Persist last-verified `(seq, chain_hash)` in an external/append-only anchor; assert monotonic growth. | Tail-truncation detection test. |
| **F-021** `[V]` | Statistical validity | **Walk-forward gate counts absolute-positive folds, not folds beating the benchmark.** `approval.py:71` majority rule on `positive_folds` (return>0), not excess>0. | `approval.py:71,74-75`. | A strategy that merely rode a bull market passes the "robustness" gate without any demonstrated edge over SPY. | Redefine fold success as excess-over-benchmark>0. | Bull-market-beta strategy fails the WF gate. |
| **F-022** `[V]` | Statistical validity | **ADR-0018 "refused by construction" re-promotion gate has a legacy hole.** The identity-compare and freshness checks are conditioned on `shadowed_at` being set; a path without it skips the identity match. | `approval.py:148,161-166`. | Re-promotion could bypass the fresh-signed-artifact requirement in a legacy state. | Make identity+freshness unconditional; fail closed when `shadowed_at` is null. | Re-promotion without a fresh artifact is refused unconditionally. |
| **F-023** `[V]` | Statistical validity | **Lineage tags are self-declared outside the factory.** `register_trial` accepts any non-empty lineage with no catalog check; a test even registers `lineage='special-tag'`. | `registry.py:40-42`; `test_trial_lineage_pg.py:117-118`. | The DSR deflation count is gameable — a fresh tag resets the multiple-testing penalty (the exact ADR-0016 defect, re-opened outside the factory path). | Bind lineage to the catalog at every registration chokepoint. | Non-catalog lineage is refused. |
| **F-024** `[V]` | Statistical validity | **`pead-sue-tr` sits at authoritative `state='paper'` on evidence whose kill-trial FAILED.** Its `validation_reports` row is `verdict='approve'` with checklist recording `kill_trial 'FAILED'`. (Sleeve fraction is 0.00, so no capital — but the authoritative flag is wrong.) | Live DB `quant.strategies 13e621f1`; `validation_reports` checklist. | An authoritative label rests on failed evidence; only the 0.00 sleeve prevents deployment. | Demote to `research_shadow` or re-validate; make an approve-with-failed-kill impossible. | Approval refuses when any mandatory gate failed. |
| **F-025** `[V]` | Ops / Reliability | **Live operational reliability is failing.** Cycles missed 2026-07-16/19/20; a single unsupervised in-process scheduler on one laptop; launchd dead (TCC); a failed cycle leaves no durable record (single-txn rollback erases the failed workflow row, node results, audit events, and LLM-spend). | `scheduler.py:1-15`; `daily.py:86-104,701-703`. | The operating loop's availability rests on one process; failures are invisible and the cost breaker undercounts across retries. | Supervised process + dead-man alert; persist failure records + LLM spend in an autonomous transaction. | Dead-man / missed-cycle alert test. |
| **F-026** `[V, partially refuted]` | Ops / Execution | **Two pre-downgrade approved xsmom orders remain in the book.** Raw finding: the next cycle's settle would deploy capital despite ADR-0018. **Refuted for the automated path:** the P0.1 fail-closed bridge/settle guard blocks research_shadow settlement (verified). Residual: the stale approved orders exist and any *manual* settle path or guard regression re-exposes them. | Live DB: AMD/INTC `pending_submit` agent BUYs; P0.1 guard in `proposals.py`. | No automated capital deployment today; latent if the guard regresses or a manual path is used. | Cancel/expire the stale pre-downgrade orders; add a guard-regression test. | Settle of a research_shadow-lineage order is refused (regression test). |

---

## MEDIUM (57 findings — grouped; each is a distinct row)

**Universe / survivorship (M1-M4, M16-M19, M26-M30):** early-window PIT membership reconstructed at ~68% with departure-only gaps → residual survivor tilt (`index_membership.py:116-120`); one spell/ticker cannot model re-entrants (`0015:37`); destructive delete-then-insert membership with no content hash → validation universe not reproducible (`index_membership.py:168-192`); deployed signal gen ignores quality gates & has no min-eligible floor, idempotency freezes a partial-month cohort (`xsmom/generate.py:156-165`); no effective-dated `is_active`/`sector_gics` history (`0001:22-35`); zero-gap completeness rule drops distress-halted names FRC/SBNY (`xsmom_pit_run.py:534-550`); no price-sanity gate → CBE 34,000× series undetected (`:529-565`); delisting terminal value = final close, optimistic (`:21-30`); spinoffs partially captured (`total_return.py:1-40`).

**Fundamentals / PIT (M8-M10, M13-M15):** split-basis drift latent in surprises (`earnings_history.py`); restatement first-write-wins with original filing_date (`0026:25-37`); peer pools use current `is_active`/`sector` for historical `as_of`, 8-day-deep store (`health_score.py:143-144`); dividends/surprises no nightly refresh (`daily.py:361-373`); desk evidence bar read unbounded by clock (`live_run.py:63-72`); scorecard grades on price-return excess vs SPY (ADR-0009 class) (`scorecard.py:53-58`).

**Metrics (M21, M41):** memo scorecard SPY-relative excess is price-return both legs (`scorecard.py:310-318`); single-symbol engine double-count + stop-fill also taints validation-lane metrics (`engine.py:96`).

**Selection (M18-M20, M24-M26):** 5 India ADRs in live ranking absent from validated universe (`xsmom/generate.py:84`); no min-coverage floor at rebalance (`:156-165`); bridge doesn't verify signal belongs to the memo instrument (`bridge.py:249-266`); `mom_12_1` source pick computes 12-0 not 12-1, 50 picks snapshotted (`source_picks.py:105-108`); no winsorisation/sanity bounds on features (`momentum.py:79-97`); post-partial-ingest frozen winner set (`daily.py:99-104`).

**Risk (M35-M39):** no execution-time risk re-check / price collar at fill (`proposals.py:1110-1162`); STRESS/FACTOR/VOL not re-evaluated at approval (`:572-578`); §7 stress one-scenario-deep, rate betas 0, gate unreachable (`stress.py:56-79`); FactorCaps/stress/vol caps are code constants outside dual-confirm governance (`factor_overlap.py:8-14`); limit_set v2 still grants SPY/INDA 0.60 L2 cap post-ADR-0017 core retirement (`engine.py:95-104`).

**Statistical (M40, M42-M45):** no mechanically-reserved OOS holdout, 23 trials reuse one window (`approval.py:74-75`); register-after-run + unguarded engine (`xsmom_pit_run.py:660`); no per-trial code pin, dataset_version/hypothesis NULL on 43/51 rows (`registry.py:26-51`); grandfathering only in ADR prose, DB reads `approve` (`approval.py:99-115`); no sensitivity analyses in code (`:1092-1125`).

**Security / repro / DB (M31-M34, M46-M52):** 745 tests silently skip without Postgres, no zero-skip enforcement (`conftest.py:49-57`); grant-based immutability never exercised as the role (`0001:119-121`); invariant-6 unenforced & already violated (`ingest_picks.py:97`); two-plane-wall test blind to relative/dynamic imports (`test_boundaries.py:26-33`); no Host/CORS/CSRF hardening → DNS rebinding (`api/main.py`); all deps unpinned floor ranges, no lockfile (`pyproject.toml:6`); ~30 wall-clock calls in `atlas/` (`test_clock.py:1`); Python 3.14.4 runtime vs 3.12 pin; append-only is convention-only, INSERT-only role inert (`0001`); bars/fx upserts rewrite history dropping `quality_flags`/`ingested_at` (`ingest.py:39-49`); single-txn T0-T9 defeats checkpoint-resume, reruns re-pay the LLM desk (`daily.py:86-91`).

**Docs / paper-exec (M53-M57):** deployed generator docstring still claims "THE RECIPE, unchanged from the validated run" (`xsmom/generate.py:12-16`); 16/18 voided proposals have no `proposal.voided` audit event (`proposals.py:873-883`); no auth/CSRF on state-mutating trading endpoints (`trading.py:144-164`); a failed daily cycle leaves zero durable record (`daily.py:701-703`); LLM spend not durable across a failed day (`daily.py:86-97`).

---

## LOW (~40) & INFORMATIONAL (~27) — summary

**Low** (defence-in-depth / small magnitude): `settle_orders`/FIFO `ORDER BY` lacks a final `id` tie-break (S13); duplicate-record conflict resolution silent & direction-inconsistent (S15); FX conversion cost not modeled (S21, ~5-10bps); optional negative-denominator surfacing (S23); boundary-tie test suggested (S12); several "add a defensive guard" items across features/quality; adj_close dead column; redis declared-unused.

**Informational:** dormant tables (`quant.backtests` 0 writers, `learning.*` 0 rows, `memo_outcomes` 0 rows, `breaker_clearances` 0 rows); paper book is 100% cash across all 7 snapshots; legacy Streamlit dashboard dormant; sentiment analyst deferred; candidate signal modules are research-only; documentation/observation items.

---

### Cross-reference

This register was produced independently. It **confirms and extends** the fund's own 2026-07-20 `REVIEW_PACKAGE/REMEDIATION_BACKLOG.md` (R-02 below-gate DSR, R-10 `MUTANT_` sentinel) with newly-surfaced, verified defects — most materially: **F-001** (PIT panel wrong-era/wrong-issuer contamination, Critical), **F-003/F-004** (backtest arithmetic errors), **F-005** (DSR 1/T substitution), **F-006** (currency-mismatched alpha), **F-008** (future-dated earnings actuals), **F-020** (audit tail-truncation blind spot, probed).

---

## P2 remediation status (2026-07-21, branch `p2-critical-high-remediation`)

Original finding descriptions above are unchanged. Remediation status (proof in `ATLAS_REMEDIATION_EVIDENCE.md`):

| Finding | Status |
|---|---|
| F-003, F-004, F-008, F-011, F-013, F-016, F-017, F-022, F-023 | **FIXED** with regression tests that fail against the pre-remediation code |
| F-001 (Critical) | **PARTIAL** — unambiguous zero-era / reused-ticker contamination excluded fail-closed and verified vs ADT/VAL/MNK; full resolution needs F-002 (issuer identity) |
| F-005 | **PARTIAL** — DSR estimator variance corrected (PSR skew/kurtosis) + empirical cross-trial dispersion capability + numerical tests; threading the dispersion through the 8 runners/approval gate is the finish step |
| M31 (silent PG skip) | **FIXED** — `ATLAS_REQUIRE_PG` hard-fail + CI wired |
| F-002, F-006, F-007, F-012, F-019, F-020, F-021, F-024, F-025, F-026 | **OPEN** — not addressed; each scoped in `ATLAS_REMEDIATION_ROADMAP.md`. F-002 & F-007 are schema/data increments; F-019/F-020 change the audit backbone; F-021 changes the approval gate; each warrants a dedicated branch |

**Completion gate NOT met** (unresolved Critical/High ≠ 0). 9 High + M31 fully remediated; F-001 (Critical) and F-005 partially. The remaining ~10 High are the deep architectural / backbone / approval-gate items, deliberately not rushed. F-013 additionally requires the operator to rotate the exposed EODHD key out-of-band.

### Round-3 update (P2.13–P2.18)

Now **FIXED** (added this round): **F-021** (benchmark-relative walk-forward gate),
**F-018** (global lock order / ABBA), **F-024** (failed mandatory kill gate is
terminal — app), **F-026** (settle refuses stale non-authoritative buy — app),
**F-014** (split-factor validation), **F-015** (nightly dividend refresh).

**Running total: 15 High FIXED** (F-003, F-004, F-008, F-011, F-013, F-014,
F-015, F-016, F-017, F-018, F-021, F-022, F-023, F-024-app, F-026-app) + M31;
**F-001 (Critical) + F-005 PARTIAL**; **9 High OPEN** (F-002, F-006, F-007, F-009,
F-010, F-012, F-019, F-020, F-025). Authoritative status: `ATLAS_FINAL_REMEDIATION_EVIDENCE.md`.
Operator actions: `ATLAS_OPERATOR_ACTIONS.md`. **Gate still NOT met.**

### Round-4 update (P2.23–P2.26)

Now **FIXED** (added this round): **F-019** + **F-020** (audit hash epoch +
protected tail anchor, migration 0036), **F-006** (AUD-consistent benchmark),
**F-010** (cross-currency fcf_yield fails closed).

**Running total: 19 High FIXED** + M31; **F-001 (Critical) + F-005 PARTIAL**;
**5 High OPEN**: F-002 (issuer identity — blocked on missing identity-history
data), F-007 (versioned ingestion — past history already overwritten), F-012
(rebalance + revalidation — gated on F-002/F-007), F-009 (split-basis EPS),
F-025 (scheduler supervision). Blockers detailed in `ATLAS_FINAL_REMEDIATION_EVIDENCE.md`.
**Gate still NOT met.**

### Round-5 update (P2.30) — F-002 issuer identity (the root finding)

**F-002 → FIXED (core), residual data-blocked.** An empirical DB check corrected
the earlier "no identifiers" assumption: `market.fundamentals.payload` carries an
ISIN for 518/526 instruments, unique per instrument. Built from real data:
migration **0037** `market.instrument_identity` (bitemporal-capable,
`history_complete=false`, `is_resolved` gate) + `atlas/dcp/market_data/identity.py`
(PIT fail-closed resolver, `same_issuer`, held-position drift detection),
populated (atlas: 518 resolved / 8 unresolved / 182 no-fundamentals → no row),
wired into the fundamentals ingest, 10 regression tests. **F-001 (Critical)
strengthened** — the ADT/VAL/MNK exclusions are now identity-corroborated
(`reused_ticker_is_identity_unvouched`), not merely date-inferred.

**Residual (honestly not fixed):** the *dated identifier change-history* (multi-row
known_from/known_to) needs a vendor symbol-change feed we do not ingest — a
Principal/operator decision (`ATLAS_OPERATOR_ACTIONS.md §6`), schema shaped to
receive it. No strategy math/params/validated numbers changed.

**Running total: 20 High FIXED-or-core-fixed** + M31; F-001 (Critical)
strengthened; F-005 PARTIAL; **4 High OPEN**: F-007, F-009, F-012, F-025. Gates on
a fresh atlas_test: **pytest 1700 passed / 0 failed**, ruff clean, mypy clean
(135 files), verify-chain 1895 OK, cov-risk 100%. **Gate still NOT met.**

### Round-6 update (P2.31) — F-009 split-basis + F-025 scheduler supervision

Both built with an adversarial-review phase (6 lenses, refute-verified). F-009
drew **zero** confirmed defects; F-025's first cut drew **three** (all fixed
before commit).

**F-009 → FIXED.** `split_basis_asof` anchor (migration 0038) + look-ahead-safe
`cumulative_split_factor` + immutable read-side re-basing (`earnings_basis.py`)
reconcile a mixed-basis store onto one basis at the read horizon (per-share
DIVIDE, no double-adjust), wired into all four cross-quarter consumers. Strict
no-op on the single-fetch panel → no golden churn. 10 tests incl. the reviewer's
split-after-ingest bar.

**F-025 → FIXED.** `ops.cycle_runs` ledger (migration 0039) written OUTSIDE the
cycle transaction → failure row + LLM spend survive rollback (M56/M57);
clock-injected missed-cycle dead-man + **pid-liveness** stuck detection (a live
long cycle is never falsely killed) + restart recovery; cross-process overlap
guard; wired into `daily.main`/scheduler/`alerts.main`. 13 tests. The three
review defects — heartbeat dead-code (false-kill of a live cycle), slow startup
recovery, 7-day lookback — were fixed via pid-liveness + a heartbeat daemon +
a 90-day lookback.

**Running total: 22 High FIXED-or-core-fixed** + M31; F-001 (Critical)
strengthened; F-005 PARTIAL; **2 High OPEN**: F-007 (versioned ingestion —
history overwritten), F-012 (revalidation — gated on F-007); plus the F-002
residual (dated identity change-history — vendor decision). Gates on a fresh
atlas_test: **pytest 1723 passed / 0 failed**, ruff clean, mypy clean (136 files),
verify-chain OK, cov-risk 100%. **Completion gate: Critical=0; High open = 2
(F-007, F-012), both gated on unrecoverable overwritten history / its
revalidation — the honest blockers named in ATLAS_FINAL_REMEDIATION_EVIDENCE.md.**
