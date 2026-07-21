# Atlas — Remediation Evidence (P2)

Per-finding proof for the P2 increment. Baseline `54c55a8` (pytest 1585/0/0). Final `ed30098`: **pytest 1622 passed / 0 failed / 0 skipped** (with `ATLAS_REQUIRE_PG=1`), ruff clean, mypy clean (132 files), verify-chain 1890 events OK, cov-risk 100.00%, empty-DB→`alembic upgrade head`→`0035 (head)` clean.

---

## F-003 — entry-day return double-counting · **FIXED**
- **Original:** `engine.py:96` exit-path mark used strict `i-1 > pos_entry_i` vs the hold path's `>=`, so a position entered day i-1 and exited day i marked from the entry price, re-counting the entry-day gain booked on day i-1.
- **Fix:** unified both paths to `>=`; removed the dead first `day_ret` assignment. `atlas/dcp/backtest/engine.py`.
- **Regression:** `tests/unit/test_backtest_engine_remediation.py::test_f003_entry_day_return_counted_once` — hand-calc: enter@100→close110 (+10%), exit@target121 (121/110=+10%) ⇒ total 0.21; the defective engine gave 0.331.
- **Proof it catches the defect:** `git stash` the engine fix → the remediation suite **fails** (`comparison failed`); restore → passes.
- **Commands:** `pytest tests/unit/test_backtest_engine_remediation.py -q` → 10 passed. Re-pinned goldens: `test_backtest_engine` (1.0201→1.0579), `test_signals_trend/breakout/meanrev`.
- **Final status:** production behaviour corrected; counted exactly once.

## F-004 — impossible/optimistic stop fills · **FIXED**
- **Original:** `engine.py:86-87` filled a stop at `sell(stop)` even when the bar opened through the stop (unobtainable).
- **Fix:** `exit_px = costs.sell(min(stop, b.open))`; non-finite/non-positive open on a stop bar raises `ValueError` (fail closed).
- **Regression:** `test_f004_*` (7 cases): gap-through fills at open 90 not stop 95; open-above/at-stop fill at stop; large gap fills at 50; costs on the open; invalid-open fails closed; target/time unaffected.
- **Live-golden confirmation:** the pre-existing `test_signals_trend` stop golden had hard-coded a sell at `0.98*100.015` on a bar that was `flat(97)` (opened at 97) — an impossible fill; re-pinned to `97*0.999`. Likewise two breakout gap-through fills.
- **Final status:** the backtester is no longer more optimistic than the (already-correct) production `exits.py`.

## F-008 — future-dated earnings actuals · **FIXED (new ingestion)**
- **Original:** `earnings_history.py` admitted any parseable `reportDate` with both EPS legs; no `report_date>period_end` or `<=receipt` check — 67 live rows with `report_date > fetched_at` (the PVH case: 2026-08-25 > 2026-07-15).
- **Fix:** parse excludes `report_date <= fiscal_period_end` and (with `known_as_of`) `report_date > known_as_of`; ingest passes `now.date()`; `store_surprises` re-checks.
- **Regression:** `tests/unit/test_earnings_history_pit_guard.py` (6 cases) — admits valid; excludes the PVH future case, on/before-period-end, mixed batches; store guard skips before INSERT.
- **Residual (documented):** the 67 pre-existing rows + a DB CHECK are a follow-up (mutates stored data).

## F-011 — dead §12 momentum overlay · **FIXED**
- **Original:** `proposals.py:589,621` filtered `st.state IN ('MUTANT_no_such_state')` (a CHECK-forbidden value) — the overlay never bound.
- **Fix:** `st.state IN ('paper','live')` (matches `bridge.py` momentum-signal join).
- **Regression:** `tests/integration/test_momentum_overlay_fires_pg.py` — a paper momentum signal is attributed; a research_shadow one is not; the old MUTANT filter returns nothing for the paper case (proves the defect).
- **Non-regression:** `test_factor_overlap`, `test_policy_conformance{,_pg}` still pass (their `momentum 0.0000` uses uuid5 refs, unaffected).

## F-017 — `make replay` prod-DB contamination · **FIXED**
- **Original:** `replay.py` seeds fixtures + upserts fixture bars via `session_scope()`; the Makefile target loaded `.env` (prod `atlas`); no guard.
- **Fix:** `assert_disposable_db()` (fail closed unless `*_test`/`atlas_test*`/`replay` or `--force`); `make replay` → `ATLAS_REPLAY_DATABASE_URL` (default `atlas_test`).
- **Regression:** `tests/unit/test_replay_guard.py` — disposable allowed; `atlas`/`prod`/`production`/`postgres` refused (SystemExit); `--force` overrides.

## F-001 — wrong-era / wrong-issuer PIT splice · **PARTIAL (Critical)**
- **Original:** the `pit-sp500` panel joins interval membership to a symbol-keyed price series; with no issuer identity, a reused ticker splices a different company's bars.
- **Fix (this increment):** `series_overlaps_membership()` — fail closed: a stored series with zero bars inside the membership era can never belong to the member; wired into the panel builder to exclude such names with an explicit F-001 reason.
- **Verified against live data:** ADT (era …2016-05-03, bars 2018-01-19…), VAL (…2016-03-30, 2021-05-03…), MNK (…2017-07-26, 2022-10-27…) — all three now `series_overlaps_membership=False` → EXCLUDED.
- **Regression:** `tests/unit/test_membership_era_guard.py` — 10 adversarial scenarios (reused-after-end, before-start, straddling-with-in-era, between-spell gap, unusable row, current-member null-start, end-exclusive, empty).
- **Why PARTIAL:** the *complete* fix must distinguish a member's own pre-index history (legitimate momentum lookback input) from a reused-ticker's bars for a *held position*, represent multiple membership spells, and resolve the delisted-tail cases — all of which require the **F-002 issuer-identity subsystem** (a schema + data increment). The unambiguous contamination is closed; the rest is scoped in the roadmap.
- **Validated magnitudes:** unchanged (PIT integration goldens did not move — these series were never rankable via `is_member_on`).

## M31 — silently-skipped Postgres tests · **FIXED**
- **Fix:** `conftest.pytest_configure` raises when `ATLAS_REQUIRE_PG=1` and PG unreachable; CI sets it.
- **§9 proof (PG-less must fail):** `ATLAS_REQUIRE_PG=1 ATLAS_DATABASE_URL=…:9999/nope pytest` → **exit 4** with `ERROR: …the integration suite would silently skip. Refusing to report a green run…`. Control: unit-only run without the flag passes.

---

## Full-suite / gate evidence (final, commit `ed30098`)

| Command | Result |
|---|---|
| `ATLAS_REQUIRE_PG=1 pytest -q` | **1622 passed, 0 failed, 0 skipped**, 82s |
| `ruff check atlas tests` | All checks passed |
| `mypy` | Success, 132 files |
| `make verify-chain` (prod DB) | audit chain OK: 1890 events |
| `make cov-risk` | 100.00% branch coverage |
| empty `atlas_test` → `alembic upgrade head` → `alembic current` | `0035 (head)` |
| PG-less `ATLAS_REQUIRE_PG=1 pytest` | exit **4** (fails, no false green) |
| no live/paper orders executed | confirmed (no broker calls; DB probes on disposable `atlas_test` only) |
| secrets printed | none (redaction not yet implemented — F-013 deferred; no key value emitted by this work) |
