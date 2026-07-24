# Focused hostile review — commit `fbbd199` (P1.4 reproducibility substrate)

Scope: verify, adversarially, that `fbbd199` ("lockfile M47 + interpreter pin M49 +
injectable-time invariant M48") is correct and introduced **no** Critical/High
regression. Eight required checks below, each with exact evidence and the fix
applied. Verdict up front: `fbbd199` improved reproducibility but **over-reached on
the interpreter pin (a High regression), overstated its "all wall-clock reads"
claim, and left the verification harness able to fail from column-budget rot**. All
are now fixed on branch `p2-critical-high-remediation` (follow-up commit); no push.

---

## 1. Is `requires-python = ">=3.14"` genuinely necessary? — **NO (HIGH regression). FIXED.**

**Evidence.**
- Pre-commit declaration: `git show fbbd199~1:pyproject.toml` → `requires-python = ">=3.12"`.
- The code is designed for 3.12+: `atlas/tools/doctor.py:23` gates on `sys.version_info >= (3, 12)`.
- **`atlas/` compiles clean under CPython 3.12.13** (`python3.12 -m compileall -q atlas/` → no
  errors) — no 3.13/3.14-only syntax. The bump to `>=3.14` was inferred purely from the
  happen-to-be-installed dev runtime (3.14.4), not from any language/library requirement.

**Impact.** `>=3.14` refuses installation on 3.12/3.13 for no reason — a gratuitous compatibility
break for any consumer/CI/deployment on those versions.

**Fix.** Reverted `requires-python` to `>=3.12`; re-ran `uv lock` (now resolved for `>=3.12`,
`uv lock --check` passes). The **runtime** is pinned separately for reproduction — that is the
correct scope, exactly as this check implies.

## 2. Do prod / Docker / CI / deployment actually support 3.14? — **partly over-reached. FIXED.**

**Evidence.** `docker manifest inspect python:3.14-slim` → exists (so does `python:3.12-slim`).
GitHub `setup-python`/`setup-uv` support 3.14. So 3.14 is *available*. But `fbbd199` forced 3.14
onto the **deployment image** (`Dockerfile FROM python:3.14-slim`) and the **mypy target**
(`python_version = "3.14"`) as well — which is out of scope for "pin the dev/CI runtime" and
narrows what the deployment/ type-check target claims to support.

**Fix.** Interpreter pin is now scoped correctly:
- `.python-version = 3.14.4` and CI `setup-uv python-version: "3.14"` — the **dev/CI reproduction
  runtime** (kept).
- `Dockerfile` reverted to `python:3.12-slim`; mypy `python_version` reverted to `3.12` — the
  **supported floor** (deployment image + type-check target track the honest minimum). mypy passes
  under the 3.12 target (`Success: no issues found in 138 source files`).

## 3. Is `uv.lock` valid for clean / offline / CI installation? — **VALID, but CI/Docker didn't use it. WIRED IN.**

**Evidence.**
- Consistent: `uv lock --check` → resolves, no drift. 79 packages, 635 hash lines.
- Clean install: `uv sync --locked --extra dev --dry-run` resolves the full set (it would reconcile
  the pip-based `.venv` to the lock's exact pins, e.g. `ruff 0.15.21 → 0.15.22` — proof the current
  `pip install -e` install had already drifted from any pin).
- Offline-capable: `uv export --format requirements-txt` emits pinned `==` requirements **with
  hashes** (210 hash lines) — verifiable offline against a cache.
- **Gap:** CI ran `pip install -e ".[dev]"` (ci.yml:24) and the Dockerfile runs `pip install -e .` —
  **neither used `uv.lock`**, so the lock provided *zero* reproducibility guarantee where it matters.

**Fix.** CI now installs from the lock: `uv lock --check` (fails on drift) → `uv sync --locked
--extra dev` → `uv run {ruff,mypy,pytest,alembic}`. (Docker still `pip install -e .`; adopting the
lock there is a noted follow-on, lower priority — the deployment image is not the reproduction
surface for the cited backtest numbers.)

## 4. Does the AST wall-clock guard catch module aliases / imported aliases / indirect wrappers / dynamic access? — **aliases+wrappers YES; dynamic NO (inherent). DOCUMENTED.**

**Evidence** (`tests/unit/test_injectable_time.py`, matcher run on live snippets):

| spelling | flagged? |
|---|---|
| `from datetime import datetime; datetime.now()` | ✅ |
| `from datetime import datetime as dt; dt.now()` (aliased) | ✅ |
| `import datetime; datetime.datetime.now()` (module-qualified) | ✅ |
| `import datetime as d; d.datetime.utcnow()` | ✅ |
| `date.today()` / `from datetime import date as dd; dd.today()` | ✅ |
| indirect wrapper `def wall(): return datetime.now()` | ✅ (the wrapper's own raw call fails the guard) |
| `getattr(datetime, 'now')()` / `eval('datetime.now()')` (dynamic) | ❌ (unreachable by static AST) |
| `from atlas.core.clock import SystemClock as SC; SC().now()` | ✅ correctly NOT flagged (the seam) |

**Fix.** The alias/module-qualified false-negatives were fixed in `fbbd199`'s own review; a self-test
now asserts all 8 evasion spellings are caught. Dynamic `getattr`/`eval` is an inherent limit of any
static analysis — now **documented honestly** in the guard's module docstring (not idiomatic here;
human review backstops it).

## 5. Are injectable clocks threaded through the ACTUAL production decision/persistence paths? — **nightly cycle YES; one miss FIXED.**

**Evidence (independent deep-trace).** The nightly `run_daily_cycle` decision/persistence spine is
fully threaded through the **single injected clock** — verified site-by-site: the F-007
`atlas.knowledge_date` GUC (`market_data/daily.py:388` `set_knowledge_date(session, clock)` before
any bar/split write, whole cycle one txn), `market.fundamentals.as_of`, `quant.signals` dates,
the `source_picks` monthly cohort, reconciliation, bridge proposals, scorecard, and all audit events.

**Defect found (MEDIUM):** `atlas/ops/ingest_picks.py:209` — `_run_desk_for` constructed a **fresh
`SystemClock()`** and passed it to `run_desk`, discarding the injected clock. So an ingest run with
`--run-desk` stamps the committee memo's `created_at` + audit events off wall-clock while the
`source_picks`/`knowledge_date` rows use the injected clock — not byte-reproducible. This is a genuine
miss of `fbbd199`'s "all reads" claim (measured-never-applied path, so MEDIUM not Critical).

**Fix.** `_run_desk_for(session, clock, ...)` now uses the caller's injected clock:
`run_desk(session, clock, [ticker], source=source)`.

## 6. Does any default-`SystemClock` fallback allow hidden nondeterminism in definitive replay paths? — **replay spine deterministic; "all reads" claim was OVERSTATED. CORRECTED.**

**Evidence (independent deep-trace).** The definitive deterministic replay envelope — the
T0–T6d + T8–T9b trading spine that produces `make replay` `gate=green` — **IS fully deterministic
under `FrozenClock`**: no reachable default-`SystemClock` fallback and no module `_WALL` from the
spine (the `start_*` `clock=None → SystemClock()` defaults are console-only entrypoints no cycle node
calls; `scheduler.py`/`recipes.py` `_WALL` are not imported by `ops/daily.py`).

**But the commit's "ALL wall-clock reads" wording is overstated** — the invariant-6 guard covers
Python `datetime.now()`, **not SQL server-clock**:
- `atlas/agents/runtime/budget.py:20` reads `... WHERE created_at::date = CURRENT_DATE`, and
  `runner.py:322` inserts `research.agent_runs` with `created_at` defaulting to DB `now()`
  (`0003_research.py:25`). This is a real DB-clock reach in the **t7 desk** budget gate — but t7 is
  **excluded from deterministic replay** (skipped without a model key; live-LLM/non-reproducible
  with one), so `make replay`'s gate result is unaffected. **Pre-existing** (fbbd199 did not touch
  these files).
- `corp_action_versions.py:32` `_KD = COALESCE(NULLIF(current_setting('atlas.knowledge_date',true)…,
  now())` — the `now()` fallback is **neutralised in-cycle** (t0 always sets the GUC first); latent.

**Fix.** No spine defect to fix. The guard docstring now states its honest scope (Python reads only;
SQL `now()`/`CURRENT_DATE` is a separate surface, and the desk-budget DB-clock read is a pre-existing
hole confined to the non-deterministic desk subsystem). The `fbbd199` commit message's absolute
"ALL wall-clock reads" is corrected in the register to "all **Python** `datetime.now()`/`date.today()`
reads".

## 7 & 8. Is the test DB auto-rebuilt/isolated so column-budget rot can't cause an unreliable run, and does the full suite pass from a clean DB with no manual intervention? — **was NO (I had to `DROP` manually). FIXED.**

**Evidence.** Postgres caps a table at 1600 columns *for its lifetime* (a dropped column keeps its
`pg_attribute` slot). The migration-cycle tests do `downgrade→upgrade`, leaking one slot per run,
accumulating **across runs** on the shared `atlas_test`. The prior `conftest._ensure_test_db`
self-healed only on a **bootstrap upgrade failure** — but the rot surfaces **mid-suite** (bootstrap
is a no-op no-op at head; a later migration-cycle test tips a table past 1600 → `TooManyColumns`
cascade). This is exactly what happened during `fbbd199`'s own verification: a full run produced
**101 failed / 183 errors** and I cleared it with a **manual `DROP DATABASE`** — so the reported
"1764 passed" was **not** obtained from a clean DB unattended. (Root cause is pre-existing — backlog
R-21 — but it makes any verification run untrustworthy.)

**Fix.** `conftest._ensure_test_db` now rebuilds **proactively**: at session start it measures the
worst-case per-table `pg_attribute` count and, if it exceeds `_COLUMN_PRESSURE_LIMIT = 1200`
(≈400 slots of headroom over one run's ~160), drops+recreates `atlas_test` fresh **before** running —
so the rot can no longer surface mid-suite. The reactive on-failure rebuild remains as a backstop.

**Proof the mechanism fires** (seeded rot → unattended rebuild):
```
seeded _rot_probe with 1300 columns (> 1200 threshold)
after bootstrap: _rot_probe gone=True; alembic head=0041; max per-table cols=25
PROACTIVE REBUILD FIRED ✓
```
**Proof of point 8** (full suite from an ABSENT database, no manual step): `DROP DATABASE atlas_test`
then `pytest` → conftest creates+migrates+runs → **`1764 passed` (exit 0), zero manual intervention**.

---

## Summary — regressions/defects from `fbbd199`, all fixed

| # | Finding | Severity | Status |
|---|---|---|---|
| P1 | `requires-python >=3.14` gratuitously narrows compat (code runs on 3.12) | **High** | FIXED — revert to `>=3.12` + re-lock |
| P2 | Docker base + mypy target forced to 3.14 (deployment/floor, not dev/CI) | Medium | FIXED — Docker/mypy → 3.12; runtime pin stays 3.14 |
| P5 | `ingest_picks._run_desk_for` used a fresh `SystemClock()` (off-clock memo/audit) | Medium | FIXED — thread the injected clock |
| P3 | `uv.lock` valid but unused by CI/Docker (M47 benefit unrealised) | Medium | FIXED (CI) — `uv sync --locked` + `uv lock --check`; Docker noted |
| P7/P8 | column-budget rot → mid-suite cascade needing a manual `DROP` | **High (verification reliability)** | FIXED — proactive hermetic rebuild in conftest |
| P4 | guard blind to dynamic `getattr`/`eval` | Low | DOCUMENTED (inherent static limit) |
| P6 | "ALL wall-clock reads" overstated — SQL `CURRENT_DATE`/`now()` in the desk subsystem | Low/Note | DOCUMENTED (pre-existing; outside the replay spine) |

Gates after fixes: ruff clean, mypy clean (3.12 target, 138 files), `uv lock --check` clean,
full suite from a clean DB unattended = **1764 passed, 0 failed (exit 0)**.
