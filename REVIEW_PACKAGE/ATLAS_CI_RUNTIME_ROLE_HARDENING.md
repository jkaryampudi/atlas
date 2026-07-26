# Atlas — CI Runtime-Role Hardening (F-019 / F-020)

**Branch:** `ci-runtime-role-hardening` (commit `d8c8085`, based on `main@d6d871f`).
**Not pushed / not merged** — per the task's no-push constraint. This document records
what the branch adds and how it satisfies the CI-hardening requirement.

**Problem it closes.** The existing CI `checks` job runs everything as the database
**owner** (`atlas:atlas`, a superuser). A green CI therefore never proved that the
**deployed** posture — migrations by the owner, the app running as the least-privilege
`atlas_app` runtime, and a superuser runtime being *refused* — actually holds. The live
pre-merge instance that was found running as a superuser (pid 35500, since stopped) is
exactly the failure this leaves undetected. **No existing test was weakened;** this
branch only *adds* coverage.

---

## 1. What the branch adds

| File | Change |
|---|---|
| `.github/workflows/ci.yml` | New **`runtime-least-privilege`** job (+41 lines). Independent of `checks`. |
| `tests/integration/test_api_lifespan_runtime_role_pg.py` | New — 3 tests on the API **lifespan** role enforcement (+79 lines). |

The `checks` job is unchanged (still runs the full pytest suite, `ruff`, `mypy`,
`uv lock --check`, and the migration check).

### 1.1 The `runtime-least-privilege` CI job (end-to-end deployed posture)

Five steps, on a fresh Postgres 16 service:

1. `checkout` + `setup-uv` (Python 3.14) + `uv sync --locked --extra dev`.
2. **Migrations run AS THE OWNER** — `ATLAS_DATABASE_URL=atlas:atlas`, `alembic upgrade
   head`. This provisions `atlas_app` (0043) and the `ENABLE ALWAYS` audit triggers
   (0044), proving migrations need the owner role.
3. **App boots as `atlas_app`** — a real `uvicorn` with
   `ATLAS_DATABASE_URL=atlas_app:atlas_app_local_only`,
   `ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true`, scheduler off — then `curl`s
   `/v1/system/health` and asserts the JSON:
   `status == ok`, `db_privilege.role == atlas_app`, `is_superuser is False`,
   `least_privilege is True`, `can_bypass_triggers is False`,
   `audit_triggers_enable_always is True`.

This is the piece the pytest step *cannot* give: a genuine server process under the
runtime role, not an in-process `TestClient`.

### 1.2 The lifespan tests (`test_api_lifespan_runtime_role_pg.py`)

- `test_lifespan_starts_as_atlas_app_and_health_is_least_privilege` — booting the app
  under `atlas_app` starts cleanly and `/health` reports least-privilege.
- `test_lifespan_refuses_superuser_runtime_when_least_privilege_required` — with
  `ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true`, a **superuser** runtime is **refused** at
  startup.
- `test_lifespan_audit_wall_assertion_runs_unconditionally` — the audit-wall assertion
  (`assert_audit_wall_enforced`) runs on every boot regardless of the flag.

(The runtime URL is built with `render_as_string(hide_password=False)` — `str(URL)`
masks the password to `***` and would fail auth.)

---

## 2. The eight required proofs — where each is covered

| # | Required proof | Covered by | Job |
|---|---|---|---|
| 1 | Migrations run with the **owner** role | `runtime-least-privilege` step 2 (`alembic upgrade head` as `atlas:atlas`) | new job |
| 2 | Runtime tests use **`atlas_app`** | `runtime-least-privilege` step 3 (uvicorn as `atlas_app`) + `test_api_lifespan_runtime_role_pg.py` (`APP_URL` = `atlas_app`) + `test_db_least_privilege_pg.py` (`app_engine` fixture connects as `atlas_app`) | both |
| 3 | App **refuses superuser** runtime | `test_lifespan_refuses_superuser_runtime_when_least_privilege_required` | checks (pytest) |
| 4 | `/health` reports the **privilege posture** (flags unsafe) | `runtime-least-privilege` step 3 curl-assert + `test_api_health.py::test_health_reports_db_privilege_posture` | both |
| 5 | Runtime cannot **disable triggers** | `test_db_least_privilege_pg.py::test_cannot_disable_audit_triggers` (as `atlas_app`) | checks (pytest) |
| 6 | Runtime cannot **`SET session_replication_role='replica'`** | `…::test_cannot_set_session_replication_role` + `…::test_replica_mode_cannot_suppress_audit_triggers_even_for_owner` | checks (pytest) |
| 7 | Runtime cannot **mutate audit** (UPDATE/DELETE) | `…::test_cannot_update_audit_events`, `…::test_cannot_delete_audit_events`, `…::test_cannot_delete_the_chain_head_anchor`, `…::test_runtime_role_has_append_but_not_mutate_on_audit` | checks (pytest) |
| 8 | **Normal ops work** with `atlas_app` | `runtime-least-privilege` step 3 (`/health` == ok, app serves) + `test_db_least_privilege_pg.py::test_runtime_role_can_read_the_app` | both |

Items 5–7 are asserted **as `atlas_app`** (the `app_engine` fixture uses
`APP_URL = make_url(URL).set(username="atlas_app", …)`), so they prove the *runtime
role itself* is blocked — not merely that a guard function exists. The `ENABLE ALWAYS`
triggers (0044) additionally make item 6 hold even for the owner
(`test_replica_mode_cannot_suppress_audit_triggers_even_for_owner`).

---

## 3. What this does NOT claim

- It does **not** prove the *production* deployment is least-privilege — CI uses a
  throwaway local password. Deploying `atlas_app` with a real secret and
  `ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true` is the operator action tracked as item #3 in
  `ATLAS_POST_MERGE_OPERATOR_STATUS.md`.
- The branch is **not pushed and not merged.** Landing it (and running it on hosted CI)
  requires an explicit instruction.

**Hosted CI status: not verified from this environment.** Whether GitHub Actions has
run this job cannot be confirmed locally. The job is authored and the YAML validates;
its first hosted run happens when the branch is pushed.

---

## 4. Local pre-merge verification performed

- `test_api_lifespan_runtime_role_pg.py` — 3 tests pass locally (real Postgres,
  `atlas_test`).
- The 19 tests touching the runtime-role / audit-wall surface pass.
- `ruff check` clean on the new test file; `ci.yml` parses as valid YAML.
- A real local `uvicorn` booted as `atlas_app` returned `/health` with
  `status: ok`, `least_privilege: true`, `audit_triggers_enable_always: true`.
