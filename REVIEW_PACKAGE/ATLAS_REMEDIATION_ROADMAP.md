# Atlas — Remediation Roadmap

*Recommended sequence only — **no remediation was implemented in this review**. Increments are scoped small enough to assign to Claude Code individually. Ordered so that result-invalidating defects are fixed before any claim rests on them. Nothing here promotes a strategy, begins data-plane remediation beyond what a finding requires, or fabricates a data digest.*

**Guardrails for all increments:** never weaken a gate to make a strategy pass; goldens re-pinned only alongside the code change that corrects them, with the old/new values recorded; `xsmom-pit-tr` stays `research_shadow`; no historical numbers rewritten (restate in new exhibits, do not mutate the record).

---

## P0 — result-invalidating defects (fix before trusting any number)

**P0.1 — Backtest arithmetic** · addresses **F-003, F-004, M41**
- Objective: the single-instrument engine books each day's return once and fills gapped stops at an obtainable price.
- Files: `atlas/dcp/backtest/engine.py` (`:96` `>`→`>=`; `:86-87` stop price → `costs.sell(min(stop, b.open))`).
- Tests: hand-calc goldens (entry→next-day exit; gap-through-stop); re-pin affected trial goldens with a recorded before/after.
- Acceptance: re-introducing either bug fails a test; affected `docs/reports` magnitudes restated in a new exhibit.
- Risk if deferred: **every single-lane backtest number remains inflated** — all downstream credibility rests on this.
- Dependencies: none. *(Recommended first increment.)*

**P0.2 — Deflated Sharpe statistic** · addresses **F-005**
- Objective: DSR uses the empirical cross-trial Sharpe variance (PSR/DSR), not `V[SR]=1/T`.
- Files: `atlas/dcp/backtest/validation.py:17-30`.
- Tests: unit test vs a worked BLdP example; re-run the lineage DSR and restate.
- Acceptance: the `1/T` substitution fails the test; the honest lineage DSR is reported beside every headline.
- Risk if deferred: the fund's central statistic overstates support.
- Dependencies: none.

**P0.3 — PIT panel identity & era binding** · addresses **F-001, F-002**
- Objective: no bar contributes to a PIT panel from outside its membership era or from a reused-ticker different issuer.
- Files: `atlas/dcp/market_data/index_membership.py`, `xsmom_pit_run.py`; add an issuer-identity column (FIGI) to `market.instruments` (schema change — a migration, no data fabrication); represent multiple membership spells.
- Tests: reused/renamed-ticker cannot inject out-of-era bars; multi-spell membership.
- Acceptance: the 3 out-of-era + 24 late-bar contaminations are refused; validation magnitudes re-derived.
- Risk if deferred: the "definitive" validation panel stays contaminated — the ADR-0018 non-reproducibility, quantified.
- Dependencies: benefits from P1.1 (issuer identity) if that lands first; otherwise carries its own minimal identity column.

**P0.4 — Currency-consistent alpha** · addresses **F-006, M21, M15**
- Objective: both legs of every excess/alpha figure are in one currency.
- Files: `attribution.py`, `bands.py`, `scorecard.py`.
- Tests: identical-return AUD vs USD series → alpha≈FX drift flagged.
- Acceptance: no metric differences AUD against USD; exhibits restated.
- Risk if deferred: every alpha/approval-bar figure is FX-polluted.

---

## P1 — research integrity & reproducibility

**P1.1 — Issuer identity + symbol-change** · **F-002, M5, M6, S2, S3** — FIGI/ISIN column, `symbol_change` writer/reader, re-listing detector refusing cross-identity bar appends. *(Foundational; P0.3 may consume it.)*
**P1.2 — Bar/FX revision integrity** · **F-007, M22, M51, S1** — on content-changing upsert, bump `ingested_at` and emit `market.bar.revised`; retain raw payloads; make revisions detectable (no bitemporal rewrite required yet).
**P1.3 — Future-dated / basis-consistent fundamentals** · **F-008, F-009, M8** — ingest guard `fiscal_period_end < report_date <= fetch_date`; consistent split basis.
**P1.4 — Reproducibility substrate** · **M47, M49, M48, R6** — add a dependency lockfile; pin the interpreter; add the invariant-6 AST conformance test; replace the ~30 wall-clock calls feeding persisted data.
**P1.5 — `make replay` safety** · **F-017** — refuse a non-`*_test` DSN.
**P1.6 — Statistical honesty** · **F-021, F-023, F-022, M40, M42** — WF gate on excess-over-benchmark; lineage bound to catalog; unconditional re-promotion identity/freshness; enforced OOS holdout; register-before-run everywhere.
**P1.7 — pead-sue-tr evidence** · **F-024** — demote or re-validate; make approve-with-failed-mandatory-gate impossible.

---

## P2 — backtest realism & risk

**P2.1 — Audit chain hardening** · **F-019, F-020, M50** — fold entity/actor into `link_hash` (new epoch); persist an external tail anchor; add the owner-role immutability test (with the DB trigger that enforces it).
**P2.2 — Risk completeness** · **F-011, M35, M36, M38, M39** — restore the §12 overlay (remove `MUTANT_`); execution-time re-check / price collar at fill; re-evaluate overlays at approval; move code-constant caps under dual-confirm governance; reconcile limit_set v2 with ADR-0017.
**P2.3 — Corporate-action & data realism** · **F-014, F-015, M28, M29, M30, S24** — split-factor sanity; nightly dividend refresh; price-sanity gate; NaN/inf write-boundary; delisting haircut convention.
**P2.4 — Deployed-vs-validated convergence** · **F-012, M18, M19, M53** — monthly rebalance-sell (or re-validate the deployed construct); align the live ranking universe; correct the generator docstring overclaim; min-coverage floor at rebalance.

---

## P3 — paper-trading operational controls

**P3.1 — API authentication & hardening** · **F-016, M46, M55** — authn + step-up on state-mutating endpoints; verified approver identity; Host/CORS/CSRF hardening.
**P3.2 — Secret handling** · **F-013** — move the EODHD token to a header; scrub URLs from errors/audit; rotate the key.
**P3.3 — Operational reliability** · **F-025, M56, M57, M11, M12, M54** — supervised process + dead-man/missed-cycle alert; persist failure records and LLM spend in an autonomous transaction; emit every void event.
**P3.4 — Concurrency** · **F-018, M52** — global advisory-lock ordering; reconcile the single-transaction cycle with checkpoint-resume.
**P3.5 — CI honesty** · **M31, M32, M33, M34** — zero-skip enforcement; run the structural tests in CI; extend the boundary test.
**P3.6 — Stale order hygiene** · **F-026** — cancel/expire the pre-downgrade approved orders; add the guard-regression test.

---

## P4 — controlled real-capital readiness (gated, later)

Only after P0–P3: FX-cost + full fee/tax model reconciliation; property/fuzz tests on risk boundaries; multi-writer concurrency tests; migration up/down round-trip; a signed, reproducible validation artifact for any strategy proposed for capital (the P1-StrategyArtifact design already drafted addresses the identity/reproducibility half — but it correctly concludes data-snapshot identity is UNAVAILABLE, so **no strategy is real-capital eligible until P1.1–P1.4 land a content-addressed data snapshot**). Real capital stays gated behind human arming (Phase 7) throughout.

---

## Sequencing rationale

P0 first because those five defects invalidate the *numbers themselves*, are small and hand-verifiable, and every later claim rests on them. P1 next because reproducibility and identity are the spine of the "not yet trustworthy" verdict. P2/P3 harden realism and operations. P4 is deliberately last and gated. **Recommended first assignable increment: P0.1 (backtest arithmetic)** — smallest, fully self-contained, hand-verifiable, and it gates the credibility of every downstream figure.
