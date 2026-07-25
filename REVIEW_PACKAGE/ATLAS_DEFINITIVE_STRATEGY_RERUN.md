# Atlas — Definitive Strategy Rerun on the Corrected Baseline

**Repository:** `main@d6d871f` (== origin/main; merged tree == remediation tip `be71d7d`).
**Run mode:** research-only, on a **disposable TEMPLATE copy** (`atlas_rerun`) of the
production DB — the real `atlas` DB was never mutated (verified: momentum trials
stayed 23; orders stayed the 2 pre-existing stale ones; no orders placed).

## Final strategy verdict: **INSUFFICIENT EVIDENCE**

The corrected definitive runner **refused to run** — it fails closed at the F-001
issuer-identity coverage gate rather than grade the survivorship-correct universe on
ticker-only history. This is the F-001 remediation working exactly as designed. The
strategy's performance **cannot be measured** on the corrected system until the
external issuer-identity feed is procured.

Clean separation of concerns:

| Dimension | Status |
|---|---|
| Software correctness | **PASS** — the fail-closed guard refuses ticker-only grading instead of producing a false number |
| Strategy performance | **NOT MEASURABLE** — the definitive panel refuses; no return/DSR/WF produced |
| Research decision-grade | **NO** — cannot produce defensible PIT evidence without the identity feed |
| Paper-trading readiness | **NOT READY** (unchanged — ADR-0018 `research_shadow`, deploys no capital) |
| Real-capital readiness | **NOT READY** (unchanged — Phase 7, human-armed) |

---

## 1. Rerun objective

Rerun the definitive Atlas flagship (`xsmom-pit-tr`, 12-1 cross-sectional momentum
on the point-in-time S&P 500) from the corrected `main@d6d871f` baseline, so its
old (pre-remediation) results — no longer authoritative — are replaced by a result
the corrected system actually produces.

## 2. Exact strategy and configuration

| Item | Value | Source |
|---|---|---|
| Strategy | `xsmom-pit-tr` (family `xsmom-pit`, lineage `momentum`) | `xsmom_pit_run.py:FAMILY/LINEAGE` |
| Signal spec | `signals.xsmom.v1` SPEC v1.0.0 — Jegadeesh–Titman 12-1, monthly, equal-weight, "textbook; no search" | `atlas/dcp/signals/xsmom/v1.py:27-28` |
| LOOKBACK / SKIP / SEASONING / TOP_N | 252 / 21 / 252 / 10 (winner = top decile) | `signals/xsmom/v1.py` |
| Rebalance | monthly | SPEC |
| Transaction costs | frozen `COSTS` (impl-variant/real_run) | `xsmom_pit_run.py` |
| Deflated Sharpe | empirical cross-trial dispersion at the true lineage count (F-005) | `dcp/backtest/approval.py` |
| Approval gate | null-model p ≤ 0.05, **benchmark-relative** DSR ≥ 0.9, WF folds excess>0 (F-021) | gate builder |
| Walk-forward | purged+embargoed, k=4, horizon=40, embargo=10 | `real_run` constants |
| Benchmark | SPY **buy-and-hold total return** (ADR-0009), converted to **AUD** by the F-006 service for grading | `market_data/benchmark.py` |
| Reporting currency | AUD (portfolio); benchmark source currency USD, declared | F-006 |
| Decision date / clock | **FrozenClock derived from the last stored bar (2026-07-24)** — deterministic, NOT wall-clock | `xsmom_pit_run.py:1556-1562` |
| Command | `python -m atlas.dcp.backtest.xsmom_pit_run --total-return [--paths N]` | module `main()` |

The runner is **not** read-only (it registers a trial + emits an audit event and
has no `--dry-run`), so the rerun used a disposable TEMPLATE copy for isolation.

## 3. Code commit

`d6d871f` (merge) — content identical to `be71d7d` (verified empty diff). Fixes
present: F-001 attestation fail-closed (`identity.py`, `xsmom_pit_run.py`),
`IDENTITY_COVERAGE_FLOOR = 0.9`.

## 4. Data identities / versions (the corrected inputs)

| Input | Identity / version |
|---|---|
| Market bars | `market.price_bars_daily` source `EodhdAdapter`; **2010-01-04 .. 2026-07-24** (~16.5y), 2.47M bars, 704 instruments; per-instrument median ~4040 sessions |
| Membership | `validation.index_membership` `GSPC.INDX`; 817 rows (674 usable), earliest start 1957; fail-closed interval rule |
| Instrument identity | `market.instrument_identity` — **526 rows, 0 closed, 518 resolved, history_complete = 0** (single-snapshot; migration 0037) |
| Dividends (TR) | `market.corporate_actions` type dividend — 29,075 (SPY 66) |
| FX (AUD basis) | `market.fx_rates_daily` USD→AUD — 4,383 rates 2010-01-01 .. 2026-07-24 |
| Benchmark | SPY — 4,164 bars 2010-2026, 66 dividends (TR OK) |
| Corp-action / bar versioning | bitemporal (migrations 0040/0041); no K-pinning threaded through this runner (F-007 residual) |

Evaluation window: `WINDOW_START = 2012-07-01` .. last bar (2026-07-24).

## 5. Identity coverage (the decisive gate)

`load_pit_panel` computes issuer-identity coverage over the window's member symbols
and **refuses below `IDENTITY_COVERAGE_FLOOR = 0.9`**:

```
RuntimeError: F-001 identity coverage 500/666 member symbols resolved (< floor 90%)
— the instrument-identity feed is too sparse to grade the definitive panel;
refusing to run on ticker-only history.
```

Independent read-only re-derivation (deterministic; identical under the
least-privilege `atlas_app` runtime):

| Metric | Value |
|---|---|
| Window members (usable, in-window) | 674 |
| Members with stored bars (panel candidates) | 671 |
| **Resolved issuer identity** | **500 (74.5%)** |
| Unresolved | 171 — **73 delisted (no ISIN)** + **98 living (no ISIN)**; 0 ambiguous |
| Coverage floor 0.90 | **BELOW → panel REFUSES** |
| Of resolved, **attested** (history_complete OR a recorded break) | **0** |

Two independent reasons the corrected system yields no result:
1. **Coverage 74.5% < 90%** — EODHD serves no fundamentals (hence no ISIN) for
   delisted tickers, so the *survivorship-correct* universe (which MUST include
   departed members) is only 75% identity-resolved.
2. **0% attested** — even the 500 resolved are single-snapshot
   (`history_complete = false`, 0 breaks), so under F-001 all pre-membership
   formation bars would be dropped as `unattested` anyway.

Both are consequences of the missing **dated symbol-change / issuer-history vendor
feed** — an explicitly PENDING external condition (Principal vendor procurement).

## 6. Data exclusions / fail-closed effects

The panel never completes, so no securities are *selected*. The fail-closed
exclusions that trigger the refusal: 171 window members without a resolvable ISIN
(73 delisted + 98 living). Had coverage passed, the attestation rule would then have
dropped every member's pre-membership formation history (0 attested identities).

## 7. Benchmark and FX basis

Corrected: SPY **total-return** (dividends reinvested) converted to **AUD** via the
one authoritative F-006 service (`market_data/benchmark.py`), PIT FX, fail-closed on
missing FX. (Not reached in this rerun — the panel refused first — but it is the
basis the corrected gate/scorecard would use, versus the old **price-return USD**
benchmark.)

## 8. Backtest assumptions (corrected, in code but not exercised this run)

Entry-day return counted once (F-003); stops fill at min(stop, open) (F-004);
split-factor quarantine (F-014); nightly dividend refresh (F-015); future-earnings
PIT guard (F-008); monthly rebalance exits (F-012); currency-safe ratios (F-010);
benchmark-relative WF gate (F-021); corrected DSR (F-005); least-privilege runtime
(F-019/F-020). None were reached — the panel refused at the identity gate.

## 9. Corrected metrics

**None produced.** The definitive panel refused (INSUFFICIENT EVIDENCE). No return,
Sharpe, DSR, drawdown, turnover, WF folds, endpoints, or approval outcome exist for
the corrected baseline.

## 10. Old-versus-new comparison

| Metric | OLD (2026-07-13, pre-remediation) | ADR-0018 (2026-07-20) | NEW (`main@d6d871f`) |
|---|---|---|---|
| Basis | split-adjusted **PRICE** return | corrected TR re-score | AUD **total return** (would be) |
| Universe (first rebalance) | ~339 reconstructed (survivor-tilted; true ≈500) | same data | **refuses** (75% ID coverage) |
| Identity | ticker-only (false continuity admitted) | ticker-only | **fail-closed; refused** |
| Strategy return | **+596.92%** | (not re-published) | **not measurable** |
| SPY benchmark | +443.76% (price) | — | not measurable |
| Sharpe / max DD | 0.76 / −36.71% | — | not measurable |
| null p / DSR | 0.000 / **0.998 (n=1)** | DSR ≈0.85; **honest 0.752** (F-005) < 0.90 gate | not measurable |
| Walk-forward | 4/4 positive (absolute) | — | benchmark-relative (F-021), not reached |
| Verdict | **PASS** (flawed basis) | **DOWNGRADED → research_shadow** | **INSUFFICIENT EVIDENCE** |

**What caused the change (PASS → INSUFFICIENT EVIDENCE):**
- **F-001 fail-closed identity gate** (decisive): the corrected system will not grade
  the survivorship-correct universe on ticker-only identity; 171 members lack ISINs
  and 0 identities attest history. The old PASS *depended on* admitting that
  ticker-only / false-continuity history and a survivor-tilted early universe — both
  now removed.
- The AUD-TR benchmark (F-006), corrected DSR (F-005), benchmark-relative WF (F-021),
  and the other corrections would further lower the old headline, but the panel never
  reaches them.

This is **not** a bug or a regression — it is bias and impossible-assumption removal.
The old +596.92% was inflated by survivorship tilt and price-return (no dividend
reinvestment on either leg) and rested on ticker-only identity.

## 11. Approval-gate result

Not reached — the panel refused before gate evaluation. No approval is produced or
implied. (The strategy's standing state remains ADR-0018 `research_shadow`.)

## 12. Final strategy verdict

**INSUFFICIENT EVIDENCE.** The corrected system cannot produce defensible
point-in-time evidence for the definitive S&P 500 momentum backtest because issuer
identity resolves for only 74.5% of the survivorship-correct member universe (below
the 90% safety floor) and 0% of resolved identities attest their pre-membership
history. This is gated on the external, PENDING dated symbol-change / issuer-history
feed. The fail-closed refusal is correct behavior, not a defect.

## 13. Usability

- **Research (decision-grade):** NO — no definitive backtest can be produced until
  the identity feed is procured (or coverage/attestation otherwise reaches the bar).
- **Paper trading:** NO — the strategy is ADR-0018 `research_shadow` and deploys no
  capital; unchanged.
- **Real capital:** NO — Phase 7, human-armed; unchanged.

The corrected system's refusal is the honest outcome: it declines to manufacture a
number it cannot defend, exactly as the F-001 remediation intends.
