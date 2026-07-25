# Atlas — Post-Merge Operator Status (main@d6d871f)

Status of external/operator actions after the merge. The repo can prove that the
**app-side code, migrations and fail-closed guards landed**; it **cannot** prove any
runtime/external mutation (rotating a vendor key, setting env secrets, mutating DB
rows, cancelling live orders, procuring a vendor feed, deploying infra). Every such
item is therefore `PENDING` or `NOT VERIFIABLE FROM LOCAL REPO`. No secret values are
printed. **No external mutations were performed** (no key rotation, no env secrets set,
no DB rows changed, no orders cancelled, no vendor feed procured, no push/merge/tag).

## 0. Actions performed this session (local, safe, reversible)

Three *local* safety actions were taken and directly verified:

1. **Stopped the stale unsafe API process.** The pre-merge API instance (pids
   35500/35502) that was running on port 8001 as a **superuser** DB role was stopped.
   Port 8001 is now free (`lsof -iTCP:8001` → no listener; no `uvicorn`/`atlas.api`
   process). This removes the live superuser-runtime exposure; it does **not** by itself
   deploy the safe runtime (see item #3, still pending).
2. **Bounded `atlas_app` smoke test.** A short-lived API boot under the least-privilege
   `atlas_app` runtime returned `/v1/system/health` = `status: ok`, `least_privilege:
   true`, `audit_triggers_enable_always: true`, `is_superuser: false`. Confirms the
   merged code runs correctly under the intended runtime role. The instance was then
   stopped; nothing was left running.
3. **CI-hardening branch created** (`ci-runtime-role-hardening`, `d8c8085`, not pushed)
   — a CI job + lifespan tests proving the deployed least-privilege posture end to end.
   See `ATLAS_CI_RUNTIME_ROLE_HARDENING.md`.

Identity-coverage investigation (F-001): the definitive panel resolves **74.5% <
90%** and the gap is unreachable from existing data (0 wireable identifiers, external
dated feed required). The flagship rerun therefore remains **INSUFFICIENT EVIDENCE**;
no rerun was performed and no gate was lowered. See
`ATLAS_IDENTITY_COVERAGE_ANALYSIS.md`, `ATLAS_IDENTITY_RECOVERY_EVIDENCE.md`,
`ATLAS_IDENTITY_DATA_REQUIREMENTS.md`.

## 1. External action status

| # | Action | Finding | Status | Evidence / note |
|---|---|---|---|---|
| 1 | **Rotate + revoke exposed EODHD key** | F-013 | **PENDING (urgent)** | Code redaction landed (0 canary leaks); the key historically leaked into the audit chain/logs and must be rotated at the vendor. Not verifiable from repo. |
| 2 | **Configure production `ATLAS_API_TOKEN`** (or per-role) | F-016 | **PENDING** | Mutators fail closed (503) without it; deny-by-default auth landed. Setting env secrets is runtime. |
| 3 | **Deploy `atlas_app` least-privilege runtime** | F-019/F-020 | **PENDING (unsafe process STOPPED this session)** | Migration 0043/0044 + secure-by-default landed. The stale superuser instance (pid 35500) was **stopped** this session (§0) and the merged code was **smoke-tested under `atlas_app`** (`/health` least_privilege:true). Still pending: the *durable* deployment — restart the production API on `main@d6d871f` as `atlas_app` with `ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true` and a real secret. Nothing is currently running. |
| 4 | **WORM / signed audit anchor** | F-019/F-020 (infra) | **PENDING** | Audit is tamper-EVIDENT + now replica-proof at the DB layer; an external WORM/signed anchor is unchanged infra hardening. |
| 5 | **Scheduler dead-man alert target `ATLAS_ALERT_URL` + hourly sweep** | F-025 | **PENDING** | Supervision code landed (0039 `ops.cycle_runs`, `ops/supervise.py`); the alert destination + cron are runtime config. |
| 6 | **Source dated symbol-change / issuer-history feed** | F-002 / **F-001** | **PENDING (blocks the strategy)** | Principal vendor-procurement decision. Until procured, `history_complete=false` for all identities and delisted members have no ISIN → **the definitive panel refuses (INSUFFICIENT EVIDENCE, 74.5% < 90%)**. |
| 7 | **ISIN backfill for ~8 living instruments (e.g. BNY)** | F-002b | **PENDING** | Targeted fundamentals re-fetch so they stop resolving to None. Part of the 98 living-no-ISIN members. |
| 8 | **Cancel / void 2 stale pre-downgrade orders (AMD, INTC)** | F-026 | **PENDING** | Two `pending_submit` xsmom BUYs approved before the ADR-0018 downgrade; settle now refuses to fill them, or cancel via authenticated API. Present in `trading.orders` (count 2, unchanged). |
| 9 | **Demote stale `pead-sue-tr` authority to research_shadow** | F-024 | **PENDING (governance)** | Authoritative `state='paper'` on evidence whose 2016 kill trial FAILED; Principal must withdraw the ADR-0013 override, then run the audited downgrade tool. |
| 10 | **Non-USD equity onboarding** | forward | **PENDING (forward)** | Would extend the AUD total-return treatment to dossier beta/RS; unreachable today (universe 511 USD + 1 AUD). |
| 11 | FX versioning (bitemporal `fx_rates_daily`); per-runner K-pinning | F-007 residuals | **PENDING (engineering, Principal-scoped)** | `upsert_rate` still overwrite-in-place; `known_by=K` not threaded through all runners. Not data-blocked. |
| 12 | Purge 3 synthetic placeholder momentum trials | F-005 residual | **PENDING (optional cleanup)** | 3 sharpe-only rows inflate `lineage_count('momentum')` 20→23; only conservatively deflate DSR. Real `atlas` shows 23. |
| 13 | Backups + verified restore drill | ops | **PENDING** | launchd jobs dead (macOS TCC); a working backup + restore drill is unproven. |

Not-applicable / closed-by-data:
* Pre-0040-cutover overwritten bars (F-007) — **NOT APPLICABLE**: no action recovers
  them; the doc states "no action closes it."

## 2. Pending actions (summary)

All of #1–#13 above are pending or not-verifiable-from-repo. The engineering side is
complete per `ATLAS_OPERATOR_ACTIONS.md` ("Still-open findings needing engineering:
None"); the remainder are operator / Principal / infra / vendor-data actions.

## 3. Blocking actions before deployment (running the app safely)

1. **Deploy the `atlas_app` runtime** (#3) — the live instance is currently a
   superuser on pre-merge code; restart on `main@d6d871f` as `atlas_app`.
2. **Set `ATLAS_API_TOKEN`** (#2) — else all mutators 503.
3. **Rotate the EODHD key** (#1).
4. **Set `ATLAS_ALERT_URL`** (#5) for dead-man alerts.

## 4. Blocking actions before paper trading

All of §3, plus:
5. **Cancel/void the 2 stale AMD/INTC orders** (#8).
6. **Demote `pead-sue-tr`** to research_shadow (#9) if it is not to trade on failed
   evidence.
7. (Governance) confirm no strategy is authoritative on un-validated evidence.

## 5. Blocking actions before real capital

All of §3–§4, plus:
8. **WORM / signed audit anchor** (#4).
9. **Dated symbol-change / issuer-history feed** (#6) — required for a defensible
   definitive backtest (the flagship is INSUFFICIENT EVIDENCE without it) and for
   real-money PIT integrity.
10. A fresh **signed validation artifact** re-promoting `xsmom-pit-tr` out of
    `research_shadow` (ADR-0018) — impossible until #6 lets the definitive backtest
    run. Live trading remains Phase 7, human-armed.

## 6. Safe operator commands (illustrative — run in the real environment; NO secrets here)

```bash
# (3) provision + deploy the least-privilege runtime
#   run migrations AS THE OWNER, then point the runtime at atlas_app
make migrate                                   # uses ATLAS_MIGRATION_DATABASE_URL (owner)
psql -c "ALTER ROLE atlas_app PASSWORD '<REAL-SECRET>'"
#   set in the deployment .env (gitignored):  ATLAS_DATABASE_URL=...atlas_app...
#                                             ATLAS_DB_REQUIRE_LEAST_PRIVILEGE=true
#   restart the API; /v1/system/health must show audit_triggers_enable_always:true,
#   least_privilege:true, and NOT 'degraded'.

# (2) production API token (fail-closed until set)
#   set ATLAS_API_TOKEN=<REAL-SECRET> (or per-role ATLAS_API_TOKEN_<ROLE>) in .env

# (1) rotate EODHD key at the vendor, then update ATLAS_EODHD_API_KEY in .env; restart

# (8) cancel the 2 stale orders via the authenticated API (TRADE_APPROVER token)
#   POST /v1/trading/orders/{order_id}/cancel  (or leave for the settle path to void)

# (5) dead-man alerts
#   set ATLAS_ALERT_URL=<endpoint> ; schedule the hourly supervise sweep

# (6) issuer-history feed — Principal vendor procurement (no local command)
```

**None of §1's external mutations were executed.** The only actions taken this session
were the local, reversible ones in §0: the stale superuser API process was **stopped**
(port 8001 now free; nothing running), the merged code was **smoke-tested under
`atlas_app`** and then stopped, and a CI-hardening branch was created (not pushed).
Durably restarting the API on the merged code as `atlas_app` (item #3) remains an
operator decision — the scheduler is currently **not running**, so the 23:30/00:30 UTC
cycles are paused until the operator restarts the API with `.env` sourced.
