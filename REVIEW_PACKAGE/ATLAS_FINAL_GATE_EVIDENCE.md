# Atlas — Final Gate Evidence

Reproducible evidence for each closure. Commands run from repo root with
`ATLAS_DATABASE_URL` pointed at the local Postgres; the full suite was run against
a **dropped-and-rebuilt** `atlas_test` (clean migrations incl. 0043).

---

## 0. Quality gates (from-scratch DB)

```
# clean rebuild
DROP DATABASE atlas_test;                       # conftest rebuilds via alembic upgrade head
ATLAS_REQUIRE_PG=1 pytest -q                     # -> exit 0, ~1868 tests, 0 failed
ruff check atlas tests migrations                # -> All checks passed!
mypy                                             # -> Success: no issues found in 141 source files
```

---

## F-016 — deny-by-default auth on every mutator

* Policy: `atlas/api/auth.py` — `Role` enum, env token→roles, `hmac.compare_digest`,
  503/401/403 posture, `SENSITIVE_ROUTE_ROLES` (16 mutators).
* Tests: `tests/unit/test_api_auth.py` (14) + `test_console_token_injection.py` (3).

```
pytest tests/unit/test_api_auth.py tests/unit/test_console_token_injection.py -q
-> 17 passed
```

Key assertions: unconfigured→503; missing/malformed→401; unknown/wrong-role→403;
admin clears all; per-role scoping (operator cannot approve trades or clear
breakers); `test_route_security_inventory` walks the live app and fails if any
mutator is unclassified/unprotected (`assert len(live_keys) >= 15`, non-vacuous);
constant-time-compare guard; console token injected server-side, never in the
on-disk file.

---

## F-013 — one secret-safe EODHD transport

* `eodhd.py` + `fxlab/ingest.py`: single `_request()`; scrubbed error raised
  OUTSIDE the except handler (chain severed).
* Tests: `tests/unit/test_eodhd_secret_safe.py` (6 fns / 35 parametrized cases).

```
pytest tests/unit/test_eodhd_secret_safe.py -q
-> 35 passed
```

AST guard: `self._client` touched only in `_request` per client; `httpx.get/post`
banned. Canary across connect-refused/read-timeout/DNS/pool-timeout/401/403/429/500
for `fetch_bars`, `fetch_fundamentals`, `fetch_earnings_calendar`, `fetch_eurusd`:
the raised `RedactingError` contains `***REDACTED***`, never `SUPERSECRET_…`, and
`__cause__ is None` / `__context__` carries no token. No real key is printed.

Whole-tree sweep confirms no other query-string-token transport (the LLM clients
pass secrets in HTTP headers, which httpx does not embed in error messages).

---

## F-006 — one AUD total-return benchmark service

* `atlas/dcp/market_data/benchmark.py` (new); consumers: scorecard, bands,
  attribution, source-picks.
* Tests: conformance (4) + `test_benchmark_service_pg` (4) + scorecard regression.

```
pytest tests/unit/test_benchmark_service_conformance.py \
       tests/integration/test_benchmark_service_pg.py \
       tests/integration/test_scorecard_pg.py -q
-> passed
```

Regression that bites (`test_excess_is_aud_total_return_not_local_currency`):
NDIA +10 % local, SPY flat, INR/AUD depreciates 10 % → stored excess `-0.010000`
(AUD), and `!= 0.100000` (the buggy local value), and the BUY is graded NOT
vindicated. Conformance test forbids `total_return_series` + `fx_to_aud` in any
module but `benchmark.py`.

---

## F-010 — shared currency normalisation for ratios

* `atlas/dcp/research/ratios.py` (new); `financials_panel` / `valuation_models` /
  `health_score` delegate.
* Tests: `test_ratios.py` (9) + `test_financials_panel_pg.py` (+2).

```
pytest tests/unit/test_ratios.py tests/integration/test_financials_panel_pg.py -q
-> passed
```

`test_fcf_yield_blocks_on_currency_mismatch`: INR statements / USD listing →
`fcf_yield_pct is None`, `fcf_yield_currency_blocked is True`, raw FCF still shown.
Confirmed-same (USD/USD) → computes, flag False.

---

## F-001 — issuer identity on every formation bar

* `identity.admit_pre_era_bars_by_issuer`; `load_pit_panel` gate + coverage floor.
* Tests: `test_pit_identity_gate_pg.py` (3) + panel coverage tests.

```
pytest tests/integration/test_pit_identity_gate_pg.py \
       tests/integration/test_xsmom_pit_run_pg.py -q
-> passed
```

Same-issuer pre-era history KEPT; reused-ticker (identity-break) pre-era DROPPED as
`wrong_issuer`; unresolved member's pre-era DROPPED fail-closed; the coverage gate
RAISES ("identity coverage 0/11 … refusing to run on ticker-only history") when the
feed is empty. Every pit fixture world now seeds resolved identities so legitimate
history remains and existing pins hold.

---

## F-019/F-020 — least-privilege runtime defeats the trigger bypass

* migration `0043` + `ops/sql/provision_runtime_role.sql`; `db_privilege.py`;
  lifespan + `/health`; docker-compose → `atlas_app`.
* Tests: `test_db_least_privilege_pg.py` (12) + `test_api_health.py` (+1).

Reproduced directly (connected AS `atlas_app`):

```
connected as: atlas_app  is_superuser: off
  SET session_replication_role=replica            : BLOCKED (InsufficientPrivilege)
  ALTER TABLE audit.decision_events DISABLE TRIGGER: BLOCKED
  UPDATE audit.decision_events                    : BLOCKED
  DELETE audit.decision_events                    : BLOCKED
  DROP TRIGGER decision_events_append_only        : BLOCKED
```

```
pytest tests/integration/test_db_least_privilege_pg.py tests/unit/test_api_health.py -q
-> passed
```

The guard passes for `atlas_app` and raises for the superuser owner; `/health`
reports the posture and never 500s on the probe.
