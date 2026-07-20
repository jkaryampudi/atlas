# BACKTEST_REPRODUCIBILITY — can the cited numbers be re-derived?

> Anchored to `REPOSITORY_SNAPSHOT.md` (commit `2ba38c0`). Governing rule from `EVIDENCE_BASE.md`:
> a result is only *reproduced* if this pass connected the exact run to its configuration, repository
> commit, and data snapshot and re-executed it. **This pass reproduced none of the historical
> performance figures**, for the reasons recorded below. This is a statement about *what could be
> verified in a safe, local, non-mutating pass* — not a claim that the numbers are wrong.

## 1. Status rubric (per the task spec)

`EXECUTED SUCCESSFULLY` · `EXECUTED WITH FAILURES` · `NOT EXECUTED — MISSING DATA` ·
`NOT EXECUTED — MISSING CREDENTIALS` · `NOT EXECUTED — EXCESSIVE COST OR RUNTIME` ·
`NOT EXECUTED — UNSAFE SIDE EFFECT` · `NOT EXECUTED — COMMAND NOT FOUND`.

## 2. The reproducibility linkage that exists in the repo (and the part that does not)

**What IS pinned.** The trial registry (`atlas/dcp/backtest/registry.py:26-51`) records, per backtest:
`strategy_family`, `spec_hash` (a canonical content hash of the recipe spec), `metrics`, `lineage`
(required since ADR-0016), and **optional** `hypothesis` and `dataset_version` (feature-store hash).
The recipe spec is content-addressed (`spec_hash`; EV-08 reproduced a stable `spec_hash` `99d193f1…`
under `--dry-run`). Seeds are pinned in code: the null model and bootstrap use **seed 7**
(`xsmom_run.py:248,498,549`; `xsmom_pit_run.py` default `--paths 1000`).

**What is NOT pinned — the reproducibility gap.**
- **No git commit SHA / code version is recorded on a trial.** The registry row has no `code_version`
  column (`registry.py:46-48`). A cited metric cannot be tied to the exact code that produced it.
- **`dataset_version` is optional and NULL for historical rows** — the registry docstring says so
  explicitly: "Both default to None — historical rows honestly stay NULL" (`registry.py:12-15`). So
  the *data snapshot* behind a historical number is generally not recoverable from the registry.
- **The market data itself is a mutable, licensed, single-vendor store** (EODHD), incrementally
  ingested (`atlas/dcp/market_data/daily.py`). There is no immutable, versioned, hash-addressed data
  snapshot per backtest. Re-running later runs against a *different* (updated) data store.
- **No `make` target reproduces a backtest.** The only backtest-adjacent Make targets are `backup`
  and `install-ops`; there is no `make backtest`/`make gauntlet` (grep of `Makefile`). Reproduction
  depends on knowing the exact `python -m …` invocation, spec file, data snapshot, and commit — none
  bundled as one artifact.

**Consequence:** a historical figure such as `+737.31%` (EV-11) cannot be *reproduced* in the strict
sense — the tuple *(exact code commit + exact CLI args + exact immutable data snapshot + registered
run row)* does not exist as a retrievable, re-runnable artifact. Re-running today would **generate a
new result** (against updated data, on newer code, registering a *new* trial), not reproduce the cited
one. This is the single most important reproducibility finding.

## 3. Per-backtest reproducibility record

For each: the invocation, what is/ isn't pinned, and the honest status *for this non-mutating pass*.

### B-1 — Recipe gauntlet, dry-run (the one thing actually executed) — **EXECUTED SUCCESSFULLY**
- **Command:** `python -m atlas.dcp.factory.recipe_run --spec <recipe>.json --dry-run`
  (`recipe_run.py:71,787-788`).
- **Working dir / env:** repo root; `.venv`; no `ATLAS_DATABASE_URL` needed (dry-run asserts no DB).
- **Config / version / commit:** recipe spec content-hashed to `spec_hash`; commit `2ba38c0`.
- **Data / period / universe / benchmark / capital / costs / seed:** dry-run **plans only** — it
  validates the spec and prints the plan (seed 7, 1000 paths) **without** touching data or the DB
  (`recipe_run.py:722-730`, `dry_run_plan`).
- **Output:** deterministic `spec_hash` (`99d193f1…`), gauntlet params echoed, "no trial registered,
  no database" (EV-08).
- **What this proves / does not:** proves the spec is content-addressed and the plan is deterministic.
  Does **not** execute the backtest or produce performance metrics.

### B-2 — Flagship `xsmom-pit-tr` full gauntlet (behind +737.31% / DSR 0.995 / WF 4/4) — **NOT EXECUTED — MISSING DATA / UNSAFE SIDE EFFECT / EXCESSIVE RUNTIME**
- **Command:** `python -m atlas.dcp.backtest.xsmom_pit_run --paths 1000 --total-return --report <path>`
  (`xsmom_pit_run.py:1441-1515`; default `--paths 1000`; seed 7).
- **Why not executed (all three apply):** (a) **MISSING DATA** — requires the licensed EODHD
  point-in-time panel; the historical snapshot behind the cited run is not recoverable (§2). (b)
  **UNSAFE SIDE EFFECT** — a real run registers a trial (`register_trial`) and writes to the dev DB.
  (c) **EXCESSIVE RUNTIME** — full 1000-path null + purged walk-forward over the S&P 500 panel.
- **Reproducibility verdict:** the *capability* to re-run exists (parameterized CLI), but the *cited
  numbers* are **not reproducible from this snapshot** (no commit/data-snapshot linkage; §2).

### B-3 — Momentum v1 real-data run (documented FAILURE) — **NOT EXECUTED here; result CLAIMED in-repo**
- **Command:** `python -m atlas.dcp.backtest.real_run …` (module `real_run.py` present).
- **Result in repo:** `docs/reports/first-real-backtest-momentum-v1.md` records that momentum v1
  **failed the gates on real data** (both SPY and AVGO). This is a *documented, honest failure* and a
  positive signal for gate integrity — but this pass did **not** re-execute it; classification
  **CLAIMED** (verdict recorded verbatim per the project's working-style rule).

### B-4 — PEAD / quality / impl-variant / validation-ETF runs — **NOT EXECUTED — MISSING DATA / UNSAFE SIDE EFFECT**
- Modules present: `pead_pit_run.py`, `quality_pit_run.py`, `impl_variant_run.py`, `xsmom_run.py`
  (validation ETF universe), `candidate_run.py`. Each registers trials and needs the licensed panel.
- Their verdicts (PEAD suspended-at-0%; quality/low-vol/etc. graveyard FAILs) are **CLAIMED** in
  package docs `00`/`04`/`08`/`16`; not re-executed here.

### B-5 — `make replay DATE=…` deterministic replay (CLAUDE.md "gate=green") — **NOT EXECUTED — UNSAFE SIDE EFFECT**
- `atlas/dcp/market_data/replay.py` uses `session_scope()` (`:13,:27`) → writes the dev DB / audit
  chain (EV-10). The "gate=green" claim is **CLAIMED**, unverifiable in a non-mutating pass.

## 4. Summary

| Backtest | Status (this pass) | Cited result reproducible from snapshot? |
|---|---|---|
| B-1 recipe gauntlet `--dry-run` | **EXECUTED SUCCESSFULLY** | N/A (plans only; determinism confirmed) |
| B-2 flagship `xsmom-pit-tr` full | NOT EXECUTED — MISSING DATA / UNSAFE / RUNTIME | **No** — no commit+data-snapshot linkage |
| B-3 momentum v1 real (failed) | NOT EXECUTED (result CLAIMED in-repo) | No |
| B-4 PEAD / quality / impl / ETF | NOT EXECUTED — MISSING DATA / UNSAFE | No |
| B-5 `make replay` | NOT EXECUTED — UNSAFE SIDE EFFECT | No |

**Bottom line for the reviewer:** the system has *strong process determinism* (content-addressed
specs, pinned seeds, lineage-scoped trial counting, a passing overfit-canary) but **weak run-level
reproducibility**: no per-run code-commit pin, an optional/often-NULL data-snapshot pin, a mutable
single-vendor data store, and no one-command reproduction path. Closing this gap (record `git_sha` +
an immutable data-snapshot hash on every trial row) is in `REMEDIATION_BACKLOG.md`.
