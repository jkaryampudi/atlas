# Atlas — Final Merge-Blocking Remediation

**Branch:** `p2-critical-high-remediation`
**Scope:** close the 7 remaining merge-blocking Critical/High findings through real
production paths (no doc-only closures). Medium-tier P2/P3 work NOT started. No
push / merge / tag. No live or external paper orders placed.

Commits: `P2.30`–`P2.36` (`f8fd8ba`, `f4eec27`, `1b9557b`, `3c52b50`, `3687911`,
`92c0270`, `08db195`).

---

## 1. Verdict

All seven findings are **FIXED** and proven by tests that exercise the real
production paths. Unresolved Critical/High application-code findings: **0**.

| Finding | Sev | Title | Status | Commit |
|---|---|---|---|---|
| F-001 | Critical | PIT panel admits ticker-only pre-index history | **FIXED** | P2.34 |
| F-006 | High | Scorecard benchmark excess currency-inconsistent | **FIXED** | P2.32 / P2.36 |
| F-010 | High | fcf_yield divides statement-ccy by listing-ccy | **FIXED** | P2.33 |
| F-013 | High | EODHD requests bypass the redacting transport | **FIXED** | P2.31 |
| F-016 | High | Sensitive mutators unauthenticated | **FIXED** | P2.30 |
| F-019 | High | Audit append-only bypassable by superuser runtime | **FIXED** | P2.35 |
| F-020 | High | Monotonic anchor bypassable by superuser runtime | **FIXED** | P2.35 |

Quality gates on a **from-scratch** `atlas_test` (dropped, rebuilt via
`alembic upgrade head` including new migration 0043):

* `pytest` — **green, exit 0** (~1868 tests; +57 new across 10 files).
* `ruff check atlas tests migrations` — **All checks passed**.
* `mypy` (strict on `atlas/core` + `atlas/dcp` + `atlas/fxlab`) — **no issues, 141 files**.

---

## 2. The 29-item final report

1. **Assignment understood** — close F-001/F-006/F-010/F-013/F-016/F-019/F-020
   through real paths; no Medium/P2/P3; no push/merge/tag; no orders.
2. **F-016 root cause** — mutating routes carried no auth; the finding named three,
   but the fix inventories *every* route.
3. **F-016 fix** — `atlas/api/auth.py`: deny-by-default, role-based
   (operator/risk_admin/trade_approver/system_internal), env token→roles,
   constant-time compare, 503 unset / 401 missing / 403 no-role / 403 wrong-role.
4. **F-016 completeness** — `SENSITIVE_ROUTE_ROLES` classifies all 16 mutators;
   `test_route_security_inventory` fails if a live mutator is unclassified or
   unprotected (a new mutator cannot ship open).
5. **F-016 control surface** — the console stays the sole surface: `/console`
   injects the loopback token at serve time (never in the static file); fetch
   helpers attach it; unconfigured → mutators 503.
6. **F-013 root cause** — `fetch_earnings_calendar` + `fetch_fundamentals` called
   `self._client.get()` directly; the token rides in the URL query string, which
   httpx embeds in every error.
7. **F-013 fix** — ONE `_request()` transport per client; every call routes
   through it; the scrubbed `RedactingError` is raised OUTSIDE the except handler
   so neither `__cause__` nor `__context__` can reach the key.
8. **F-013 extra** — found and closed a SECOND EODHD path the review did not name:
   `fxlab/ingest.py`.
9. **F-013 guard** — AST conformance: `self._client` is touched only in
   `_request` per client class; `httpx.get/post/...` banned in both modules.
10. **F-013 canary** — connection-refused / timeout / DNS / pool-timeout / 401 /
    403 / 429 / 500 across four call sites; the raised error carries the MASK,
    never the fake token, chain severed. No real key printed.
11. **F-006 root cause** — the scorecard subtracted a raw-USD SPY *price* return
    from the instrument's *own-currency price* return; sibling consumers had
    grown three separate AUD/TR copies.
12. **F-006 service** — `atlas/dcp/market_data/benchmark.py`: the ONE authoritative
    reporting-basis return (AUD, total-return, split-adjusted, PIT-FX).
13. **F-006 wiring** — scorecard, demotion bands, daily attribution, and the
    source-pick edge all grade against numbers from the service; three inline
    copies deleted.
14. **F-006 conformance** — a test forbids the reporting-basis pattern
    (`total_return_series` + `fx_to_aud` in one module) anywhere but the service.
15. **F-006 regression** — a non-USD memo with a moving currency: the AUD
    conversion FLIPS the excess sign (+0.10 local → −0.01 AUD), so the buggy value
    is impossible under the fix.
16. **F-006 signals scoped** — regime label and relative-strength rank stay
    currency-native by design (converting them injects spurious FX); documented.
17. **F-010 root cause** — `fcf_yield_pct = 100·FCF/market_cap` mixes
    statement-currency FCF with listing-currency market cap (false yield for an
    ADR), emitted as a silent number.
18. **F-010 layer** — `atlas/dcp/research/ratios.py`: `cross_currency_ratio`
    returns `(value, blocked)` — computes only when currencies are confirmed
    equal, else an EXPLICIT block (never a silent zero).
19. **F-010 wiring** — `financials_panel` (fcf_yield + `fcf_yield_currency_blocked`
    flag), `valuation_models`, and `health_score` all route through the shared
    layer; inline copies deleted.
20. **F-010 inventory** — audited every ratio: vendor multiples (PE/EV-EBITDA/…)
    and inverses are single-currency by construction; valuation upside is behind
    the currency-blocked panel; only fcf_yield was Atlas-formed cross-currency.
21. **F-001 root cause** — pre-index formation bars were kept with no issuer
    check; a ticker reused before the member joined could splice foreign history
    into the momentum signal.
22. **F-001 gate** — `identity.admit_pre_era_bars_by_issuer`: in-era bars are
    attested by membership; a pre-era bar is kept only if it resolves to the
    member's issuer, else dropped fail-closed (wrong-issuer or unvouchable).
23. **F-001 coverage** — `load_pit_panel` refuses to run (RAISES) when < 50 % of
    ranked members resolve an issuer identity — no grading on ticker-only history.
24. **F-001 evidence** — same-issuer history KEPT, reused-ticker pre-era DROPPED,
    unresolved DROPPED, empty-feed panel REFUSED; legitimate history verified
    across every pit fixture world (xsmom/tr/pead/quality/recipe/factory/low-vol).
25. **F-019/F-020 root cause** — the audit triggers (0042) are real only against a
    non-superuser runtime; production connected as the superuser owner `atlas`,
    which bypasses via `SET session_replication_role='replica'` (reproduced).
26. **F-019/F-020 role** — migration 0043 provisions `atlas_app` (non-superuser,
    non-owner, least grants); `ops/sql/provision_runtime_role.sql` is the DBA form.
27. **F-019/F-020 enforcement** — `assert_least_privilege_runtime` fails the API
    startup closed and marks `/health` degraded when the deployment demands least
    privilege but the runtime can bypass; docker-compose points the app at
    `atlas_app`.
28. **F-019/F-020 proof** — connecting AS `atlas_app`: SET session_replication_role,
    DISABLE/DROP TRIGGER, UPDATE/DELETE audit, DELETE chain_head, ALTER TABLE — ALL
    refused; SELECT + audit INSERT still work.
29. **Verification** — from-scratch `atlas_test`: full `pytest` green (exit 0),
    ruff clean, mypy strict clean; 23-vector hostile self-review in
    `ATLAS_FINAL_GATE_SELF_REVIEW.md`; no survivors.

---

## 3. Per-finding change map

* **F-016** — `atlas/api/auth.py` (rewritten), routers `system/risk/research/factory`
  gated, `atlas/api/main.py` `/console` injection, `atlas/dashboard/console.html`
  (3 fetch helpers), `tests/conftest.py` (bypass all `AUTH_DEPENDENCIES`),
  `tests/unit/test_api_auth.py` (14), `tests/unit/test_console_token_injection.py` (3).
* **F-013** — `atlas/dcp/market_data/adapters/eodhd.py`, `atlas/fxlab/ingest.py`,
  `tests/unit/test_eodhd_secret_safe.py` (6 fns / 35 cases).
* **F-006** — `atlas/dcp/market_data/benchmark.py` (new), `scorecard.py`,
  `trading/bands.py`, `reporting/attribution.py`, `research/source_picks.py`,
  `tests/unit/test_benchmark_service_conformance.py` (4),
  `tests/integration/test_benchmark_service_pg.py` (4), scorecard regression test,
  fixture FX seeding.
* **F-010** — `atlas/dcp/research/ratios.py` (new), `financials_panel.py`,
  `valuation_models.py`, `health_score.py`, `tests/unit/test_ratios.py` (9),
  `tests/integration/test_financials_panel_pg.py` (+2).
* **F-001** — `atlas/dcp/market_data/identity.py`
  (`admit_pre_era_bars_by_issuer`), `backtest/xsmom_pit_run.py` (gate + coverage
  floor + PitUniverse fields), `index_membership.py` (docstring),
  `tests/integration/test_pit_identity_gate_pg.py` (3), pit-fixture identity seeding.
* **F-019/F-020** — migration `0043`, `ops/sql/provision_runtime_role.sql`,
  `atlas/core/db_privilege.py` (new), `atlas/core/config.py`,
  `atlas/api/main.py` (lifespan), `atlas/api/routers/system.py` (`/health`),
  `docker-compose.yml`, `tests/integration/test_db_least_privilege_pg.py` (12),
  `tests/unit/test_api_health.py` (+1).

---

## 4. Documented residuals (external conditions, not app code)

* **Audit WORM anchor** — the monotonic anchor still lives in the same DB; a
  cluster superuser with out-of-band access remains an external WORM-store /
  key-signing decision (unchanged from 0042). The runtime can no longer reach it.
* **M46 reads-auth** — read routes stay loopback-open (a Medium deployment
  condition, explicitly out of scope). Only mutators are gated.
* **Dossier beta / relative-strength** — display-only risk/momentum indicators in
  panel space; the current active universe is **511 USD + 1 AUD, zero non-USD
  equities** (NSE coverage is zero), so they are single-currency today. They feed
  no sizing/grading (invariant 2). If non-USD equities are ever added, they inherit
  the same treatment — a forward condition, noted, not a live defect. The
  authoritative grading path is AUD-TR regardless.
* **EODHD credential rotation** — external ops action; the app no longer leaks it.
