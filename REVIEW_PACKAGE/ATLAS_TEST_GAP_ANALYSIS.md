# Atlas — Test Gap Analysis

*Reviewed at `d65e0b1`. 1,585 tests pass locally (Postgres up). The suite is genuine and mostly outcome-asserting — but several of the system's strongest *claimed* protections have no test that would fail if the protection broke. Gaps are prioritised; P0 = a defect exists today that a test would have caught.*

## The meta-gap: silent skipping

**M31 — ~745 tests (the entire `tests/integration/` + constitution suites) skip when Postgres is unreachable** (`tests/conftest.py:49-57` `requires_pg`), and **CI has no zero-skip assertion**. Locally they ran (PG up); a PG-less or misconfigured CI would report green having exercised almost none of the fail-closed, audit, snapshot, lifecycle, or PIT machinery. **This gap sits above all others** — it determines whether any of the structural tests run at all.

**Fix first:** add a CI step asserting `0 skipped` (or a required-PG marker that errors, not skips, in CI). Acceptance: a CI run with no DB fails loudly.

---

## P0 — tests that would have caught a live defect

| Gap | Finding | Test to add | Acceptance |
|---|---|---|---|
| No test hand-verifies backtest arithmetic | **F-003, F-004** | Golden: entry→next-day exit books the return exactly once; gap-through-stop fills at `min(stop, open)`, hand-computed | Deliberately re-introducing `>`/stop-at-stop fails the test |
| DSR statistic untested vs a worked example | **F-005** | Unit test of the DSR/PSR against a Bailey–López de Prado worked case | `V[SR]=1/T` substitution fails |
| Audit **tail-truncation** untested | **F-020** (probed) | Delete the last N events → verification MUST fail | Tail delete detected |
| Audit hash coverage untested | **F-019** (probed) | Mutate `entity_id`/`actor_id` → verification MUST fail | Detected after folding columns into `link_hash` |
| Owner-role immutability untested | **P3/P7, M32, M50** | Connect as the app role, attempt UPDATE/DELETE on audit + bars → MUST be blocked (needs the DB trigger that doesn't exist yet) | UPDATE/DELETE raises at the DB |
| PIT panel identity untested | **F-001, F-002** | Reused/renamed ticker cannot contribute out-of-era bars; a symbol change remaps to the same UUID | Wrong-era/wrong-issuer rows refused |
| Future-dated earnings actual untested | **F-008** | Ingest a row with `report_date > fetch_date` → refused/flagged | Future actual never stored as a fact |
| Currency-mismatched alpha untested | **F-006** | Identical-return series in AUD vs USD → alpha≈FX drift is flagged | Mixed-currency differencing fails |

---

## P1 — research-integrity & reproducibility

| Gap | Finding | Test to add |
|---|---|---|
| Invariant-6 has no conformance test | R6, **M48** | AST scan banning naked `datetime.now()`/`utcnow()`/`date.today()`/`time.time()` in `atlas/` (allow-list the clock) |
| Two-plane wall blind to relative/dynamic imports | **M34** | Extend `test_boundaries.py` to catch `from ..agents`, `importlib.import_module`, `__import__` |
| WF gate not benchmark-relative | **F-021** | A bull-beta strategy (all folds positive, none beats SPY) must FAIL the WF gate |
| Lineage self-declaration | **F-023** | `register_trial` with a non-catalog lineage is refused |
| Re-promotion legacy hole | **F-022** | Re-promotion with `shadowed_at` null still enforces identity+freshness |
| In-place bar revision undetected | **F-007, M51** | Re-ingesting a changed bar emits a `market.bar.revised` event / bumps `ingested_at` |
| `make replay` target-DB guard | **F-017** | Replay against a non-`*_test` DSN is refused |
| No OOS holdout enforcement | **M40** | A trial evaluated on the reserved window cannot also have trained on it |
| Register-after-run / unguarded engine | **M42** | An aborted run still burns a trial; the raw engine refuses to run unregistered |

---

## P2 — data & corporate-action realism

| Gap | Finding | Test to add |
|---|---|---|
| Split-basis drift in earnings surprises | **F-009, M8** | A split after first ingest keeps the EPS series on one consistent basis |
| Cross-currency ADR ratios | **F-010** | A non-USD reporter's health/valuation ratios use one currency |
| Price-sanity gate | **M29** | A 34,000× daily move (CBE-class) is quarantined, not ranked |
| NaN/inf write-boundary | **S24, M25** | `materialize()` refuses non-finite / absurd feature values |
| Dividend freshness | **F-015, M13** | The daily cycle refreshes dividends; staleness alarms |
| Duplicate-conflict surfacing | **S15** | Vendor duplicates with differing values are counted/audited, not silently last-wins |
| Delisting terminal value | **M30** | Involuntary-delisting return uses a documented haircut convention |
| Membership reproducibility | **M3** | Re-fetching the constituent snapshot is refused or re-pins a content hash to trials |

---

## P3 — operational & execution controls

| Gap | Finding | Test to add |
|---|---|---|
| API endpoint auth | **F-016, M55** | State-mutating endpoints reject unauthenticated / unverified-approver calls |
| Host/CORS/CSRF | **M46** | Cross-origin / bad-Host requests to the API are rejected |
| Advisory-lock ordering | **F-018** | A forced ABBA acquisition does not deadlock (global order enforced) |
| Execution-time risk re-check | **M35** | An order whose book breached between approval and fill is stopped at fill |
| Overlay re-check at approval | **M36** | STRESS/FACTOR/VOL re-evaluated at approval time |
| Dead-man / missed-cycle alert | **F-025** | A missed daily cycle raises an alert |
| Durable failure + spend records | **M56, M57** | A failed cycle persists a failure row and the LLM spend outside the rolled-back txn |
| Void audit events | **M54** | Every proposal void emits a `proposal.voided` event |

---

## P4 — controlled real-capital readiness (later)

Property/fuzz tests for the risk engine boundaries; concurrency tests for multi-writer ingestion; migration up/down round-trip test (CI applies from zero — add a downgrade path test); a golden immutable regression corpus for the corrected backtest metrics; end-to-end FX-cost reconciliation; secret-scrubbing test (adapter errors never surface the token, **F-013**).

---

## What is already well-tested (keep)

Risk engine (100% branch coverage, fail-closed paths), audit payload-tamper + interior-deletion detection, tie-break determinism, liquidity fail-closed (L10), NaN-absence in the live store, feature `code_sha` pinning, PEAD no-look-ahead effective-index, next-open fill + shortfall, reconciliation=kill, FIFO lot disposal, sha256-pinned memo-eval fixtures, the overfit canary (proves the gates reject junk). These are genuine and should be preserved as the corrections land.
