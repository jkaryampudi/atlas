# POINT-IN-TIME & BIAS ANALYSIS — the evidence pass on backtest integrity

> Anchored to `REPOSITORY_SNAPSHOT.md` (commit `2ba38c0`, review `2026-07-20T08:41:38Z`) and
> `EVIDENCE_BASE.md`. Read those two first. This document is read-only inspection: no code, test,
> config, or data was modified, and no state-mutating command was run to produce it.
>
> **Governing rule (restated).** Code existence is not verified behaviour. A guard, docstring, or
> config that *describes* a protection earns at most **CLAIMED** or **NOT TESTED** — never
> **VERIFIED** — unless an `EVIDENCE_BASE` section-A item executed it or the exact enforcing line was
> read. This pass executed nothing here; classifications are overwhelmingly **INFERRED / CLAIMED /
> NOT TESTED / NOT FOUND**, with **VERIFIED** used only for exact source lines actually read.
>
> **No verdict.** This document assesses point-in-time correctness and backtest-inflating bias. It
> gives **no** investment or production-readiness judgement, and introduces **no** performance number
> not already in the repository.

## The one structural fact that governs every row

There are **two universes and two return conventions** in this codebase, and they do not match:

| | Universe source | Return convention | Delisting | Where |
|---|---|---|---|---|
| **Approval / validation backtest** | point-in-time S&P 500 membership (`validation.index_membership`, `is_member_on`) | **total return** (dividends reinvested) | delisting-aware (`_liquidate_dead`) | `atlas/dcp/backtest/xsmom_pit_run.py` |
| **Deployed live sleeve (what actually trades paper)** | **current** `market.instruments.is_active` set | **split-adjusted price return** | none (dead names already inactive) | `atlas/dcp/signals/xsmom/generate.py` |

The clean point-in-time / total-return / survivorship treatment lives in the **validation plane only**.
The **deployed ranker** that writes `quant.signals` each night — and therefore every forward paper
result — uses the current active list and price return. This divergence is the root of rows 2, 3, 5,
and 7, and the code itself admits it (`generate.py:15-22`, ADR-0010 caveat 3: the implementable-variant
backtest that would close the gap is "board item 5, OPEN").

## Risk table

| Risk | Protection claimed | Actual implementation | Evidence | Test coverage | Residual risk | Severity |
|---|---|---|---|---|---|---|
| **1. Look-ahead bias** (strategy sees only `bars[:i+1]`) | "No-look-ahead is STRUCTURAL": the strategy view is physically clamped and execution is at the *next* session's open. | Backtest: `PanelView.close(s,i)` raises `"look-ahead: session {i} > view clamp {t}"` for `i > t` (`atlas/dcp/backtest/portfolio.py:113-120`); single-instrument engine calls `strategy(bars[:i+1])` (`atlas/dcp/backtest/engine.py:104`); chosen weights execute at the next open, not the decision close (`portfolio.py:234-243`, docstring `:5-9`). **Live ranker uses a different mechanism**: SQL date caps `pb.bar_date = ANY(:window)` ending at the signal session and `ca.action_date <= :d` (`atlas/dcp/signals/xsmom/generate.py:189,206`), plus a signal session defined as the latest *stored* US bar ≤ last completed session (`generate.py:156-165`). | **EV-14 (INFERRED, traced not executed).** Exact clamp line read: `portfolio.py:117`. Dedicated property test *exists* but was **not executed this pass**: `tests/unit/test_portfolio_engine.py:145` `test_no_look_ahead_is_structural` (docstring: "perturbing FUTURE prices never changes" the result). | `test_portfolio_engine.py:145` (structural property) + `tests/unit/test_xsmom_pit_engine.py` clamp/eligibility cases exist; **not run this pass**. The **live** SQL-clamp path (`generate.py`) has **no dedicated look-ahead test** found. | Backtest clamp is strong and exact-line-confirmed. The *deployed* ranker relies on a separate, un-look-ahead-tested SQL clamp; correctness there depends on ingest never back-writing a later-dated bar under the cap — untested here. `assert` in the WF driver is `-O`-strippable (see row 9). | Low |
| **2. Survivorship bias / delisting inclusion** | Backtest panel is delisting-aware; "dead companies are the point of this test." | Approval run KEEPS early-ending series and force-liquidates at the final close (`xsmom_pit_run.py:255-275` `_liquidate_dead`; loader keeps dead series, `:426-431`). **But the general (non-PIT) `xsmom_run.load_universe_panel` drops early-ending series** (stated at `xsmom_pit_run.py:427-428`), i.e. is survivorship-biased. **The live sleeve ranks the current `is_active` universe** — `i.is_active AND i.market='US' AND i.instrument_type IN ('stock','adr')` (`generate.py:186-188`); dead/removed names are already inactive and invisible. `seeds/universe.json` is a **static current-membership manifest** (512 rows; keys `symbol, exchange, market, instrument_type, name, sector_gics, currency, economic_exposure`; **no as-of / start / end / valid field**). | **INFERRED** from traced code. Live universe survivorship confirmed by exact lines (`generate.py:186-188`) + manifest schema read (`seeds/universe.json`). PIT backtest delisting confirmed by exact lines (`xsmom_pit_run.py:255-275`). | `tests/unit/test_xsmom_pit_engine.py:73` `test_forced_liquidation_hand_pinned` + equivalence-to-frozen-engine cases exist; **not run this pass**. No test covers survivorship of the **live** `is_active` universe (there is none to test — it *is* survivorship-biased by construction). | The clean treatment protects **only** the validation backtest. Every **forward** paper result is generated on the survivorship-biased current list. Any historical claim generated through `xsmom_run` (non-PIT) inherits the drop-dead-series bias. | High |
| **3. Point-in-time universe MEMBERSHIP reconstruction** (can it rebuild the historical *investable* universe as of each past rebalance?) | ADR-0016 / index-membership module claim PIT reconstruction: "a ticker is a member of the index on day D iff … `(start ≤ D) AND (end > D)`." | A PIT membership table **does exist** in the validation plane: `validation.index_membership` (migration 0015, sealed) with the interval rule `is_member_on(row, day)` (`atlas/dcp/market_data/index_membership.py:123-131`) and `usable()` (`:116-120`). **However:** (a) it is **not used by the live ranker** (`generate.py` uses `is_active`, not membership); (b) it is a **single mutable vendor snapshot** — `replace_membership` does DELETE-then-INSERT per index (`index_membership.py:168-192`), not append-only/versioned, so a re-fetch silently rewrites "as-of" history; (c) **fail-closed exclusion**: a departed/delisted row with a NULL start date is EXCLUDED ENTIRELY as an "unknowable interval" (`usable()`, `:116-120`; docstring `:16-26`), systematically dropping members whose join is unrecorded; (d) membership before **2012-07-01 is refused** as unreliable (`WINDOW_START`, `:64`); (e) documented **ticker-reuse confusion** (e.g. `ALTR`, `AGN`) (`:22-26`; `activate_universe.py:12-16`). Live activation flips **current** constituents active (`activate_universe.py:130-207`, `is_active_now`). | **INFERRED** (mechanism traced, exact lines read). For the **live/deployed path** the reconstruction is **NOT FOUND** — no as-of membership is consulted by `generate.py`, and `seeds/universe.json` carries no dates (row 2). Ties to **EV-11** (the flagship run's data linkage is not reconstructable). | `tests/unit/test_index_membership.py:37-54` exercises `is_member_on` interval boundaries (start/end exclusivity); **not run this pass**. **No** test asserts the live sleeve trades a PIT universe. | **MAJOR LIMITATION.** The historical investable universe as the *live system would have traded it* cannot be reconstructed: the deployed ranker uses today's static list, and the only PIT table is (i) validation-plane-only, (ii) a single mutable current vendor snapshot re-derivable to *different* history on re-fetch, (iii) fail-closed-lossy on unknowable intervals, (iv) blind before 2012, (v) ticker-reuse-contaminated. Any historical performance claim rests on a universe that is either survivorship-biased (live path) or reconstructed from a non-immutable snapshot (validation path). | High |
| **4. Corporate actions / split adjustment** | "Bars strictly BEFORE a split's effective date are divided by the ratio … multiple splits compound. Deterministic, pure, property-tested." | `adjust_for_splits` divides prices / multiplies volume for `bar_date < action_date`, compounding across splits, tagging `split_adjusted` (`atlas/dcp/market_data/adjustment.py:14-37`). Raw bars stored; adjusted **on read** (`real_run.py:73` `load_adjusted_obars`). Splits ingested nightly (`atlas/dcp/market_data/daily.py:255-261` `fetch_splits`/`record_split`). Dividends split-adjusted with the identical rule (`total_return.py:56-76`). | **INFERRED** (code traced; exact adjustment line `adjustment.py:22-24`). | `tests/unit/test_adjustment.py`, `tests/integration/test_split_adjusted_reads_pg.py` exist; **not run this pass**. | The **math** is sound and tested; the **exposure is vendor data quality** — a single vendor (EODHD) supplies split dates/ratios with **no independent reconciliation** in-repo. A missed or mis-dated split silently corrupts formation returns and rankings. Adjustment recomputed on every read (no immutable adjusted series). | Medium |
| **5. Dividend / total-return timing** | ADR-0009's binding benchmark is SPY **total return**; the recipe was validated on total return. | **Approval ran total return** (`xsmom_pit_run.py` `total_return=True`; `total_return.py:111-148`, reinvest at ex-date close). **The deployed ranker uses split-adjusted PRICE return**: `c_skip / c_form - 1.0` on `pb.close` (`generate.py:183-222`), no dividend term. **Dividend ingest is manual-only**: the nightly cycle ingests "bars + splits + FX" and **never fetches dividends** (`daily.py:1`, and `grep fetch_dividends daily.py` → 0 hits); dividends have a **standalone CLI** (`atlas/dcp/market_data/dividends.py:29-33`) that is **not scheduled** in `ops/`, `api/`, or `Makefile` (searched, none). The live SPY-TR benchmark recomputes forward from stored dividends (`bands.py:158-176` `_spy_tr_close`). | Code facts **VERIFIED (exact-line)**: live ranker uses price close (`generate.py:219-222`); nightly ingest has no dividend fetch (`daily.py`, grep 0). Consequence (benchmark decay in production) **INFERRED**, not executed on a live series this pass. | `tests/unit/test_total_return.py` covers the TR transform; **not run this pass**. **No** test asserts live-vs-approval convention parity, and none exercises dividend staleness. | Two problems: (i) **methodology deviation** — the deployed sleeve does not compute the return it was validated on (cross-sectional momentum is less dividend-sensitive, but it is still not the validated recipe); (ii) because dividends are hand-loaded, the SPY-TR series **decays toward price-return** as new (uningested) SPY ex-dates pass, **understating the benchmark** — which biases the excess-vs-SPY metric *in the strategy's favour* (see row 7). | High |
| **6. Restatement / revision bias (fundamentals)** | — (value/quality sleeve is future work) | **No PIT fundamentals vendor is wired**; value/quality factors are unbuildable at this commit. Scaffolding exists (`atlas/dcp/market_data/quarterly_fundamentals.py`, `estimate_snapshots.py`) and the feature store has a **stale-fact / in-place-revision guard** via content-hashed `dataset_version` (`atlas/dcp/features/store.py:45,66`), but nothing populates PIT fundamentals for a live factor. | **EV-18 (NOT FOUND / PLANNED).** Feature-store revision guard exists (`store.py:66`) — CLAIMED, not exercised for fundamentals. | Feature-hash tests exist (`tests/unit/test_feature_hash.py`); no PIT-fundamentals restatement test (nothing to test). | Not applicable to the **current** deployed book (price-only momentum), so restatement bias cannot inflate today's results. Becomes a **live High** the moment a value/quality sleeve reads point-in-time fundamentals — the whole class of look-ahead (using restated figures as if known at the time) is unmitigated because the data layer does not yet exist. | Low |
| **7. Benchmark point-in-time integrity (SPY TR)** | SPY total return, PIT-consistent, computed identically on both sides of every comparison. | In the **backtest**, SPY is loaded into the same panel and TR-transformed with every holding, and is **asserted out of the ranked universe** (`xsmom_pit_run.py:503-505`, `BENCHMARK="SPY"` `:144`). In **live scoring**, the demotion band and attribution compare against SPY TR from stored bars + stored dividends (`bands.py:83,158-176`; `attribution.py:67-82,274-287`). Band history is read from **stored** `spy_tr_close` rows, "replayable; never recomputed history" (`bands.py:49-52`). | **INFERRED** (traced; exact lines read for benchmark exclusion `xsmom_pit_run.py:503-505` and stored-row replay `bands.py:49-52`). | `tests/integration/test_attribution_daily_pg.py`, `test_low_vol_family_pg.py` touch the TR benchmark; **not run this pass**. | Stored history is PIT-honest, but the **forward** SPY-TR leg depends on manually-ingested dividends (row 5). A stale dividend feed makes each new `_spy_tr_close` **omit recent SPY ex-dates**, understating the benchmark. This directly feeds the ADR-0010 demotion trigger (`trailing_126_session_excess_vs_spy_tr_pp`): an understated benchmark **inflates measured excess and biases *against* demotion** — the failure the accountability loop is meant to catch. | High |
| **8. Data-snapshot immutability / reproducibility** | "Every backtest registers a trial"; deflated Sharpe uses the true count. | `register_trial` pins `family, lineage, spec, metrics` (`registry.py:26-51`; call at `xsmom_pit_run.py:697-702`). The spec records `"data": "EODHD real"` as **free text** (`xsmom_pit_run.py:695`) — **no content hash** of the bars, dividends, or membership actually used; **no `dataset_version` is passed** by the momentum runs (searched `xsmom_pit_run.py`/`real_run.py`/`candidate_run.py`). Underlying market data is **mutable**: bars re-ingestable, dividends hand-added, membership DELETE-then-INSERT (`index_membership.py:179-181`). A `dataset_version` content hash exists **only** in the feature store (`store.py:45`), which the price-momentum runs do not read. | **EV-11 (CLAIMED figures; reproducibility UNKNOWN).** Absence of a data-snapshot binding is **NOT FOUND** — `register_trial` has no snapshot arg wired at `xsmom_pit_run.py:697-702`. | No test binds a backtest result to an immutable data snapshot (there is no snapshot to bind). | **A backtest cannot be re-executed to the same numbers.** Re-running today reads a mutated DB (new bars, new/absent dividends, a re-fetched membership snapshot) and *generates a new result*. The flagship figures (EV-11) are not reconstructable from `(config + commit + data-snapshot + seed)` because the data leg is not pinned. Every performance claim is therefore un-auditable at the data layer. | High |
| **9. Embargo / purging in walk-forward** (k=4, horizon=40, embargo=10) | Purged + embargoed folds (López de Prado): purge train days whose label window overlaps test; embargo the days after. | `purged_folds` excludes train days with `a - horizon ≤ t < b + embargo` (`atlas/dcp/backtest/walkforward.py:25-38`); `leakage_free` re-checks the same predicate (`:41-43`) and is **`assert`-ed per fold** in both drivers (`walkforward.py:66`; `xsmom_pit_run.py:412`). Params are caller-supplied; the PIT portfolio WF pulls "constants from real_run" (`xsmom_pit_run.py:404-405`). Deflation count is lineage-scoped (`registry.py:63-70`), consistent with EV-06. | **INFERRED** (structural, traced; predicate exact lines read `walkforward.py:35-43`). WF fold pass-count (EV-11 "WF 4/4") is **CLAIMED**. | `tests/unit/test_walkforward.py:12-30` sweeps `k∈{3,4,5}`, `horizon∈{10,40}`, `embargo∈{0,10}` and asserts every fold is leakage-free and the day before test start is purged; **not run this pass**. Test targets the **single-instrument** engine, not the PIT portfolio WF. | (i) The `leakage_free` guarantee is enforced by `assert`, which `python -O` strips. (ii) The **numeric adequacy** of `horizon=40`/`embargo=10` for a *monthly-rebalanced, monthly-held* label was not independently validated here — the label/return window of a held position can exceed 40 sessions, and 10-session embargo (~2 weeks) against monthly serial correlation is not justified in code. (iii) The exact WF constants used for the flagship PIT run were not confirmed against `real_run` this pass. | Medium |

## Biases that cannot be excluded from this snapshot

Even where a protection is present and structurally sound, this read-only pass cannot **exclude** the
following. Each is a real residual, not a hypothetical:

1. **Live survivorship & universe drift (rows 2–3).** The deployed sleeve ranks the *current*
   `is_active` S&P 500 set (`generate.py:186-188`; `seeds/universe.json` has no as-of dates). Names
   that were index members on a past rebalance but have since been removed or delisted are absent from
   any forward computation, and the only PIT membership table is validation-plane-only and mutable.
   **Direction of bias: optimistic** (dead/removed names silently excluded).

2. **Non-immutable data layer (row 8).** Bars, dividends, and membership are all mutable in place; no
   backtest is pinned to a content-hashed snapshot (`xsmom_pit_run.py:695-702`). Reproducing the
   headline figures (EV-11) is impossible from this snapshot — a re-run measures *today's* data, not
   the data behind the claim. Any number carried forward from a prior run is **unverifiable at the
   data level**.

3. **Benchmark decay from manual dividend ingest (rows 5, 7).** Dividends are hand-loaded
   (`daily.py` ingests none; `dividends.py` is unscheduled). Between hand-runs the SPY total-return
   series omits new ex-dates and drifts toward price-return, **understating the benchmark**.
   **Direction of bias: optimistic** for the strategy's measured excess-vs-SPY, which is precisely the
   ADR-0010 demotion trigger — the guardrail is biased toward *not* firing.

4. **Deployed ≠ validated recipe (row 5).** Approval used total return over a PIT, delisting-aware
   universe; the live ranker uses price return over the current list. The forward paper record is not
   the strategy that was gated.

5. **Vendor-single-source corporate actions (row 4).** Split (and dividend) correctness rests on one
   vendor with no in-repo reconciliation. Split *math* is tested; split *data* is not cross-checked.

6. **Unexecuted assurances (all rows).** Every "test coverage" cell names tests that **exist but were
   not run in this pass**. Per the governing rule they raise nothing to VERIFIED. The look-ahead,
   delisting, membership-interval, TR-transform, and purge/embargo properties are all **INFERRED**,
   not confirmed by execution here (contrast the six items actually executed in `EVIDENCE_BASE.md`
   §A, none of which is a bias property).

7. **Restatement bias is latent, not absent (row 6).** It cannot affect the price-only book today,
   but no PIT-fundamentals discipline exists to prevent it the moment a value/quality sleeve ships
   (EV-18).

### Restated, in one line

**The inability to reconstruct the historical *investable universe* as the live system would have
traded it is a MAJOR limitation on any historical performance claim.** A validation-plane
point-in-time membership mechanism exists (`index_membership.is_member_on`), but it is not what the
deployed ranker uses, it is a single mutable vendor snapshot rather than an immutable per-backtest
artifact, it fail-closed-drops members with unknowable join intervals, it is blind before 2012, and it
is ticker-reuse-contaminated. Combined with a non-immutable data layer (row 8) and a survivorship-biased
live universe (row 2), no historical performance figure in this snapshot can be treated as reconstructed
against a pinned, point-in-time-correct investable universe.

## Claims that could NOT be substantiated against code (honest gaps)

- **The flagship performance figures** (+737.31% vs SPY TR +593.89%, p=0.000, DSR 0.995, WF 4/4) —
  **not substantiated here.** They are **EV-11: CLAIMED / reproducibility UNKNOWN**. This pass did not
  reproduce them and, per row 8, they are not connectable to a pinned data snapshot. (EV-06 does
  reproduce the DSR *arithmetic* from a raw Sharpe input, but not the Sharpe input itself.)
- **"WF 4/4 folds positive"** and the **exact k/horizon/embargo constants** used for the flagship PIT
  walk-forward — **not confirmed.** `xsmom_pit_run.py:404-405` defers to "constants from real_run";
  the numeric values behind the headline were not traced to their call site this pass. The
  purge/embargo *mechanism* is confirmed (`walkforward.py:25-43`); its *parameterisation for this
  label structure* is not validated.
- **Live look-ahead safety of the deployed ranker** — **not tested.** The SQL date caps in
  `generate.py:156-165,189,206` are plausible but have no dedicated look-ahead test (the property test
  at `test_portfolio_engine.py:145` covers the backtest `PanelView`, a different mechanism), and were
  not executed here.
- **Vendor accuracy of splits/dividends/membership** — **UNKNOWN.** Requires the licensed EODHD feed
  and independent reference data, neither available to this pass (`REPOSITORY_SNAPSHOT.md` §5).
- **Whether the manual dividend CLI is in fact run on any cadence in the operator's environment** —
  **UNKNOWN from code.** No scheduler entry exists in-repo; whether a human runs it is outside the
  snapshot.

---
*File written: `/Users/jayakrishnakaryampudi/Documents/atlas/REVIEW_PACKAGE/POINT_IN_TIME_AND_BIAS_ANALYSIS.md`.
Read-only pass; no application code, test, config, or data was created or modified. All classifications
follow `EVIDENCE_BASE.md`; no performance figure was reproduced or introduced.*
