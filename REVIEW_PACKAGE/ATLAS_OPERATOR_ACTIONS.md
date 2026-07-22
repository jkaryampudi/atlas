# Atlas — Required Operator Actions

Actions that must be performed by a human operator in the real environment.
Claude Code cannot and did not perform these (external credentials, production
state mutation, deployment configuration). Never paste secret VALUES into this
file or any transcript.

---

## 1. Rotate the exposed EODHD credential (F-013) — **URGENT**
- **Reason:** the `ATLAS_EODHD_API_KEY` leaked historically into the audit chain
  and logs (the reason the redaction fix went in). Redaction stops *future*
  leaks; it does not un-expose the key already leaked.
- **Procedure:** rotate/regenerate the key at EODHD; update `ATLAS_EODHD_API_KEY`
  in `.env` (repo root, gitignored); restart the API/scheduler process **with
  `.env` sourced** (`set -a; . ./.env; set +a`) or it 401s and the scheduler
  disarms.
- **Verify:** old key rejected by EODHD (a fetch with it fails); a nightly ingest
  succeeds with the new key; `make verify-chain` still green.
- **Restart required:** yes. **Role:** Principal / operator.

## 2. Demote the stale `pead-sue-tr` authority (F-024)
- **Reason:** `pead-sue-tr` is authoritative `state='paper'` on evidence whose
  pre-committed 2016 kill trial FAILED (ADR-0013). The code now makes such an
  approval impossible, but the existing row's label is still wrong. Sleeve is
  0.00 (no capital), so this is a governance-label correction, not a capital risk.
- **Procedure (governance):** reversing a signed ADR-0013 override is the
  Principal's call. When decided, run the existing audited tool:
  `python -m atlas.tools.downgrade_xsmom_shadow --family pead-sue-tr
   --expect-state paper --actor "<Principal>" --reason "F-024: failed-kill
   evidence; ADR-0013 override withdrawn" --review-reference "independent review
   F-024" --decision-ref "<new ADR>"`
- **Verify:** `SELECT state FROM quant.strategies WHERE family='pead-sue-tr'`
  returns `research_shadow`; a `quant.strategy.research_shadow` audit event exists.
- **Restart required:** no. **Role:** Principal.

## 3. Cancel the two stale pre-downgrade approved orders (F-026)
- **Reason:** two `pending_submit` xsmom BUYs (AMD, INTC) were approved before the
  ADR-0018 downgrade. The settle path now REFUSES to fill a non-authoritative
  buy (it voids it fail-closed), so they cannot execute — but they should be
  cleared from the book explicitly.
- **Procedure:** cancel each via the authenticated API
  `POST /v1/trading/orders/{order_id}/cancel` (needs the `ATLAS_API_TOKEN`
  bearer), or leave them — the next settle voids them automatically now.
- **Verify:** `SELECT count(*) FROM trading.orders WHERE state='pending_submit'
  AND side='buy'` returns 0; the proposals are `voided`; no executions/tax_lots
  were created.
- **Restart required:** no. **Role:** operator.

## 4. Configure the production API authentication secret (F-016)
- **Reason:** state-mutating endpoints now require a bearer token sourced only
  from `ATLAS_API_TOKEN`; if unset, mutations are disabled (503, fail-closed).
- **Procedure:** set a strong `ATLAS_API_TOKEN` in `.env` (never a default);
  configure the console/clients to send `Authorization: Bearer <token>`.
- **Verify:** an unauthenticated `POST /v1/trading/proposals/{id}/approve`
  returns 401; with the token, 200/expected.
- **Restart required:** yes (to load the env). **Role:** operator.

## 5. Configure the scheduler dead-man alert target (F-025 — now IMPLEMENTED)
- **Reason:** durable scheduler supervision + missed-cycle dead-man is now
  implemented (migration 0039 `ops.cycle_runs`, `atlas/ops/supervise.py`, wired
  into the scheduler tick and the hourly `alerts.main()` sweep). Set
  `ATLAS_ALERT_URL` so the pages (`cycle_missed` / `cycle_stuck`) actually reach
  you; with it unset the condition is still durably recorded (the `ops.alert.urgent`
  audit latch) but only printed to stderr.
- **Also schedule the hourly sweep** (`python -m atlas.ops.alerts`) via cron so
  the dead-man fires even when the API — and its in-process supervisor — is the
  thing that is down (the sweep runs it too).
- **Single-host assumption:** stuck-recovery uses process-liveness (the recorded
  pid), which is host-local. This is correct because the scheduler subprocess, the
  in-proc supervisor, the hourly sweep, and `make daily` all run on the SAME
  machine. If Atlas is ever split across hosts, a cross-host 'running' row is only
  recovered at the 12h absolute cap — revisit then.
- **Restart required:** yes (to load `ATLAS_ALERT_URL`). **Role:** operator.
  **Status:** feature landed; operator sets the alert URL + cron.

## 6. Issuer-identity change-history feed + ISIN backfill (F-002 residual)
- **Reason:** F-002's resolution layer is built and populated from real ISINs
  (migration 0037 + `identity.py`), but two pieces need non-code decisions:
  - **(a) Dated change-history — Principal decision.** Reconstructing a ticker's
    identifier history across issuers (multi-row `known_from/known_to`) needs a
    vendor **symbol-change / delisting feed** (EODHD current-fundamentals gives
    one snapshot). Until procured, `history_complete=false` and PIT lookups
    before an instrument's first stored bar fail closed (safe, not silent).
  - **(b) ISIN backfill — operator.** 8 *active* instruments (e.g. `BNY`) have a
    fundamentals row but no ISIN, so they resolve to None (fail closed). A
    targeted fundamentals re-fetch should populate them.
- **Verify:** after a re-fetch, `populate_identities` reports fewer `unresolved`;
  `resolve_by_symbol('BNY')` returns a resolved identity.
- **Restart required:** no. **Role:** (a) Principal / (b) operator.

---

## Still-open findings needing engineering (not operator actions)
F-007 (versioned ingestion), F-012 (rebalance-sell + revalidation — gated on
F-007), and finishing F-005 (DSR dispersion threading). **Now fixed since this
list was last written:** F-002 (core; residual is the vendor decision in §6
above), F-006, F-009, F-010, F-019, F-020, F-025. See
`ATLAS_FINAL_REMEDIATION_EVIDENCE.md` for the current per-finding status.
