# Atlas — Post-Merge Verification (main@d6d871f)

First verification after the Critical/High remediation branch merged to `main`.
Read-only + research-only; no code changes, no push/merge/tag, no orders.

## 1. Repository, branch and commit

| Item | Value |
|---|---|
| origin | `https://github.com/jkaryampudi/atlas.git` |
| branch | `main` |
| HEAD | `d6d871f5436f341198fe05883b070e05344cc5ab` |
| origin/main | `d6d871f` (**== local main; in sync**, empty diff both directions) |
| merge commit | `d6d871f` (`Merge p2-critical-high-remediation`) |
| remediation tip | `be71d7d` — **merged tree == be71d7d** (empty `git diff be71d7d..HEAD`) |

Confirmed on `main@d6d871f`, in sync with origin, tree identical to the verified
remediation tip.

## 2. Worktree status

`git status --short` → **clean** (no uncommitted changes; `.env` is gitignored and
not part of the tree). No forcing of state; the repo was already at the expected
commit.

## 3. Verification commands and results (from a dropped/rebuilt `atlas_test`)

| Command | Exit | Result |
|---|---|---|
| `pytest` (clean DB, `ATLAS_REQUIRE_PG=1`, `-rf`) | 0 | **1871 passed, 0 skipped, 0 failed, 0 errors** — no silent skips |
| `ruff check atlas tests migrations` | 0 | All checks passed |
| `mypy` (strict: core + dcp + fxlab) | 0 | no issues, 141 source files |
| `make doctor` | 0 | all clear; 48 tables, migrations applied |
| `make verify-chain` (real `atlas`) | 0 | audit chain OK: 2341 events verified |
| `uv lock --check` | 0 | Resolved 79 packages, lock current |
| `make cov-risk` (100% branch on `atlas/dcp/risk`) | 0 | **100% branch coverage** — see §3.1 |
| M31 negative (`ATLAS_REQUIRE_PG=1` + unreachable PG) | error | `UsageError: Refusing to report a green run that tested nothing structural` — zero-skip guard fires |

### 3.1 cov-risk note

`make cov-risk` runs the FULL suite with `--cov=atlas.dcp.risk --cov-branch
--cov-fail-under=100`. A first attempt errored during test SETUP (not a coverage or
assertion failure) while the definitive-strategy rerun and a leftover `make api`
scheduler were concurrently holding PostgreSQL connections; a clean re-run (with the
heavy load finished) confirms **100% branch coverage on `atlas/dcp/risk`**. `dcp/risk`
was **not touched** by the merge (the fixes changed `core/db_privilege`, `core/config`,
`market_data/identity`, `backtest/xsmom_pit_run`, `api/*`, migration 0044), so the
Phase-4 exit criterion is unchanged.

## 4. Migration results

| Step (throwaway DB) | Exit | Result |
|---|---|---|
| upgrade absent → head | 0 | lands at **`0044 (head)`** |
| downgrade head → `0042` (exercises 0043 + 0044 downgrade) | 0 | clean |
| re-upgrade → head | 0 | clean; audit triggers back to `ENABLE ALWAYS` (`tgenabled='A'`) |

CI additionally runs an apply-from-zero migration check (`.github/workflows/ci.yml`).

## 5. PostgreSQL role verification (F-019/F-020)

| Role | superuser | can run DDL (migrations) | audit UPDATE/DELETE |
|---|---|---|---|
| `atlas` (owner / migration role) | **yes** | **yes** | (blocked by trigger regardless) |
| `atlas_app` (intended runtime) | **no** | **no** | **no privilege** (SELECT+INSERT only) |

* **Audit wall is replica-proof:** both triggers are `ENABLE ALWAYS` (`tgenabled='A'`,
  migration 0044). Reproduced on a disposable schema clone: as the **owner
  superuser** in `session_replication_role='replica'`, UPDATE/DELETE on
  `audit.decision_events` and the anchor are **refused** (the pre-fix bypass is
  closed).
* **Startup fails closed:** `db_require_least_privilege` defaults **True**; the API
  lifespan **refuses to start** when connected as the superuser owner and runs an
  **unconditional** audit-wall assertion. Verified: startup as `atlas` → RuntimeError;
  as `atlas_app` → starts, `/health` reports `audit_triggers_enable_always: true`.
* **Least-privilege is functional:** `atlas_app` reads the app + the definitive
  panel's inputs and computes the identical fail-closed identity coverage (§ strategy
  rerun), and can append audit/register trials (INSERT) — no superuser needed.

⚠ **Deployment gap (observed):** a leftover `make api`/uvicorn process (pid 35500,
from a prior session, pre-merge code) is running on :8001 and its launch command
**explicitly exports `ATLAS_DATABASE_URL=atlas`**, overriding `.env` — so the LIVE
running instance is currently a **superuser runtime**. This is direct evidence that
the "runtime = `atlas_app`" operator action (Deploy §2) is still **PENDING**; the
live instance must be restarted on `main@d6d871f` connected as `atlas_app`. (Its
scheduler is the source of the periodic `audit.chain.verified` events on the real
`atlas` DB — benign.)

## 6. Hosted CI status or limitation

**Hosted CI not verified from this environment.** The `gh` CLI / GitHub Actions API
were not queried. Local inspection of `.github/workflows/ci.yml`:

* One workflow (`checks`, ubuntu-latest, on push/PR). Dedicated steps: `uv lock
  --check` + `uv sync --locked`, `ruff check atlas tests`, `mypy`, `pytest` (real
  Postgres 16 service, `ATLAS_REQUIRE_PG=1`), and an apply-from-zero **migration
  check** (`alembic upgrade head` + asserts head).
* Coverage of API auth, secret redaction, PIT identity, audit protection, and the
  `atlas_app` least-privilege role is **only indirect** — folded into the single
  `uv run pytest` step; no dedicated steps, and `make verify-chain` is not run.
* **CI-hardening follow-up (owner-creds):** CI sets `ATLAS_DATABASE_URL=
  postgresql+psycopg://atlas:atlas@localhost:5432/atlas` — the **owner** role for all
  paths. It never exercises the app running as the least-privilege `atlas_app`
  runtime (only the in-pytest privilege tests, which connect as `atlas_app`
  themselves, cover that). Recommend a CI job that boots the app as `atlas_app` with
  `ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true` and a separate migration-owner URL.

## 7. Regression-check summary

* Full suite **1871 passed / 0 skipped / 0 failed** from a clean DB — no
  Critical/High regression. All prior remediation regression tests (F-002…F-026, M31)
  are present and green (verified in the merge-gate review and re-run here).
* Hostile self-check (see `ATLAS_DEFINITIVE_STRATEGY_RERUN.md` §10 and below): no
  finding classifies as `REMEDIATION REGRESSION`. The strategy rerun's refusal is the
  F-001 fail-closed guard working correctly, not a regression.
* Hostile-check highlights: no ticker-only identity used (the panel refused); missing
  identity history failed **closed**; no manual DB repair; no silent PG skips
  (`ATLAS_REQUIRE_PG=1`); the definitive refusal is identical under the
  least-privilege `atlas_app` runtime; deterministic clock (last bar, not wall-clock);
  no secrets printed; **no orders created/approved/sent**; real `atlas` DB untouched
  by the research run.

## 8. Remaining external conditions (all PENDING — see `ATLAS_POST_MERGE_OPERATOR_STATUS.md`)

None are provable-complete from the local repo; the app-side code + guards landed,
but runtime/external mutations cannot be proven here. Highest-impact for the
strategy: the **dated symbol-change / issuer-history vendor feed** (Principal
procurement) — until then the definitive panel fails closed (INSUFFICIENT EVIDENCE).
Others: rotate the exposed EODHD key; set production `ATLAS_API_TOKEN`; deploy the
`atlas_app` runtime (observed still superuser); WORM/signed audit anchor; scheduler
`ATLAS_ALERT_URL`; cancel/void the 2 stale AMD/INTC orders; demote `pead-sue-tr`;
ISIN backfill for 8 living names; purge 3 synthetic momentum trials; FX versioning /
K-pinning (F-007 residuals).

## Verdict

* **Software correctness: PASS** — full gate green from a clean DB; migrations,
  role separation, audit-wall, and fail-closed guards all verified.
* **Deployment readiness: NOT READY** — external ops conditions pending; the live
  instance is still a superuser runtime on pre-merge code (needs restart as
  `atlas_app` on `main@d6d871f`).
