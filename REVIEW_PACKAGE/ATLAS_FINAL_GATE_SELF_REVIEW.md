# Atlas — Final Gate Hostile Self-Review (23 vectors)

Adversarial pass over the seven closures: each vector is an attempt to defeat the
fix. **Survivors are fixed, not excused.** Two survivors were found DURING
implementation and closed (V-8, V-13); the rest held.

Legend: **HELD** = attack fails against the fix. **FIXED** = attack succeeded, then
was closed.

## F-016 — auth

1. **New mutator ships unprotected** — add a POST route, forget to classify it.
   → **HELD.** `test_route_security_inventory` walks the *live* app; an
   unclassified/unprotected mutator fails the suite (`>=15` guard keeps it
   non-vacuous).
2. **Timing side-channel on the token** — → **HELD.** `hmac.compare_digest`;
   guard test forbids `==` regressions.
3. **Token leaks into logs/audit** — → **HELD.** Auth failures raise
   `HTTPException` with a generic detail; the presented token is never logged nor
   placed in an audit payload.
4. **Empty per-role env var grants a role** — `ATLAS_API_TOKEN_OPERATOR=""`.
   → **HELD.** `_role_token` returns `None` on empty (`… or None`); an empty
   presented token never matches.
5. **Unconfigured deployment is open** — → **HELD.** `auth_configured()` false →
   503 on every protected route (fail closed), proven by
   `test_unconfigured_disables_mutations_fail_closed`.
6. **Console bypass** — the console sends no token so mutators break, tempting a
   loophole. → **HELD.** `/console` injects the loopback token server-side; no
   anonymous mutator path exists.
7. **Reads exfiltrate state** — → **SCOPED.** Reads stay loopback-open (M46, a
   Medium deployment condition, explicitly out of scope); the API binds 127.0.0.1
   (ADR-0018). Only mutators are gated.

## F-013 — secret transport

8. **Token reachable via the exception chain** — `raise … from None` still leaves
   `__context__` pointing at the token-bearing httpx error. → **FIXED.** The
   scrubbed error is raised OUTSIDE the except handler, so `__cause__` and
   `__context__` are both None (asserted by the canary).
9. **A third EODHD path leaks** — → **FIXED.** Found `fxlab/ingest.py` (not named
   by the review) and gave it its own `_request`. Whole-tree sweep confirms no
   other query-string-token transport.
10. **Retry/backoff logs the raw URL** — → **HELD.** The only caller of the wire is
    `_request`; every escape is already a `RedactingError`, so even if a caller
    logs it the token is masked.
11. **Short/edge token not masked** — `redact` ignores secrets < 6 chars.
    → **HELD for real keys** (EODHD keys are 20+ chars); a 1-char "secret" masking
    the whole string is the deliberate guard, documented in `secrets.py`.
12. **AST guard is vacuous** — → **HELD.** It asserts `self._client` is used in
    exactly `{_request}` per client class *and* bans `httpx.get/post`; a bypassing
    method flips the set and fails.

## F-006 — benchmark service

13. **Ambiguous SPY silently picks one** — multiple committed SPY rows.
    → **FIXED.** The service resolves SPY to exactly one active instrument, else
    fail-closed (None → dormant). The full-suite run surfaced the bands test
    relying on the old loose "union all SPY bars"; fixed the fixture to park other
    SPYs inactive (P2.36). Production has one active SPY.
14. **Missing FX fabricates a return** — → **HELD.** `fx_to_aud` raises; the
    scorecard/source-picks catch per-symbol → fail-closed skip; direct test
    `test_reporting_close_series_fails_closed_without_fx`.
15. **A new consumer computes SPY return inline** — → **HELD.** Conformance test
    forbids `total_return_series` + `fx_to_aud` co-located outside `benchmark.py`.
16. **Flat FX hides the bug in tests** — → **HELD.** The regression test uses a
    MOVING currency and asserts the excess sign FLIPS and `!= 0.100000`; the buggy
    value is impossible.
17. **Non-authoritative signals over-converted** — forcing AUD onto a regime label
    or RS rank would inject spurious FX. → **HELD by design**; documented and
    scoped out; conformance test does not touch them.

## F-010 — currency ratios

18. **Unknown currency computes anyway** — → **HELD.** `cross_currency_ratio` uses
    `currencies_confirmed_same` (both known AND equal); unknown fails closed.
19. **Silent zero reads as a real yield** — → **HELD.** A block returns
    `(None, True)` and surfaces `fcf_yield_currency_blocked`; a missing input is a
    distinct `(None, False)` absence.
20. **Another cross-currency ratio slipped through** — → **HELD.** Inventory
    audited: vendor multiples + inverses are single-currency; valuation upside is
    behind the currency-blocked panel; only `fcf_yield` was Atlas-formed. Conformance
    test bans inline `reporting == listing` re-implementations in the three modules.

## F-001 — panel identity

21. **Reused ticker pre-index enters formation** — the exact defect.
    → **HELD.** `admit_pre_era_bars_by_issuer` drops a pre-era bar unless it
    resolves to the member's issuer; `test_reused_ticker_pre_era_bars_are_dropped`.
22. **Empty identity feed runs on ticker-only history** — → **HELD.** The coverage
    floor RAISES below 50 % resolved; `test_identity_coverage_gate_refuses_ticker_only_panel`.
23. **Ambiguous symbol→instrument sneaks bars in** — → **HELD.**
    `instrument_id_for_symbol` returns None on ambiguity → the symbol is excluded
    fail-closed before the identity gate.

## F-019 / F-020 — DB privilege (spanning check)

* **Superuser runtime bypasses the triggers** — reproduced (owner `atlas`
  succeeds). → **HELD** against `atlas_app`: SET session_replication_role,
  DISABLE/DROP TRIGGER, UPDATE/DELETE audit, DELETE chain_head, ALTER TABLE all
  refused (12 tests). Startup + `/health` fail closed when the deployment demands
  least privilege but the runtime can bypass.
* **Health probe 500s and hides the posture** — → **HELD.** `/health` wraps the
  probe; on any failure it reports `checked: false` and never 500s.

## Residuals (external, documented, not survivors)

* WORM anchor / cluster-superuser out-of-band access — external store decision
  (unchanged from 0042); the runtime can no longer reach it.
* Non-USD equities would extend the AUD-TR treatment to dossier beta/RS — a forward
  condition; unreachable today (universe is 511 USD + 1 AUD, zero non-USD equities).

**Survivors after this pass: none.**
