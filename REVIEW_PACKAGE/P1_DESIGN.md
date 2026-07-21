# P1 — Unified, Immutable & Signed StrategyArtifact — Design (Revised)

**Status:** conditionally approved; revised per the 13 mandatory changes. **Pre-implementation.**
**Branch:** `p1-strategy-artifact` (cut from tag `p0.1-baseline` @ `54c55a8`).
**Migration head at design time:** `0035` → P1 migrations begin at `0036`.

> **Governance phase, not a strategy phase.** P1 changes no strategy math, parameters, allocation,
> risk caps, stop/target policy, historical results, DSR, PIT datasets, cost assumptions, lifecycle
> semantics, scope separation, or promotion/demotion thresholds. **`xsmom-pit-tr` stays
> `research_shadow`. Nothing is promoted.**

> **Load-bearing conclusion (unchanged, mandated to keep):** Atlas has **no content-addressed data
> snapshot** for the data deployed strategies consume (`market.price_bars_daily` / `fx_rates_daily`
> are `ON CONFLICT DO UPDATE`; `quant.feature_values.dataset_version` hashes *extent*, is feature-
> store-only, and is **NULL for `xsmom-pit-tr`**). Therefore **data identities are recorded
> `UNAVAILABLE`, no data digests are fabricated, and every real strategy is
> `authoritative_eligible=false`.** Building a real content snapshot is a **post-P1 data-plane
> project** and is explicitly **out of scope** here. No strategy is promoted; no data-plane
> remediation begins.

---

## Mandate traceability

| # | Mandatory change | Where addressed |
|---|---|---|
| 1 | Separate executable **content digest** from a **signed, domain-separated validation attestation** | §2, §8 |
| 2 | Include `strategy_id`, `strategy_family`, `strategy_version`, schema version **in the content digest** | §2.1, §3 |
| 3 | Sign an **attestation** (artifact id, content digest, parent digest, target state, validation-report digest, trial-ledger digest, signer key id, signed ts, review ref, decision ref) — **not only manifest bytes** | §2.2, §8.2 |
| 4 | **Immutable runtime lineage** at signal/decision-run level (decision-run record referenced by every signal, or artifact identity on every signal) | §7.4, §10.3 |
| 5 | Harden ACJSON (bool-before-int, NFC, fixed UTC ts, canonical UUID, POSIX rel paths, symlink+mode handling, cross-process golden vectors) | §4 |
| 6 | Clean repo: hash **Git blob** contents + record **Git tree**; separately record+verify **package/container** identity | §5 |
| 7 | `authoritative_eligible` = **runtime-derived assessment with explicit blocking reasons**; do not trust a stored boolean | §10.2 |
| 8 | Replace mutable key **status** with **immutable key registration + append-only key-state events** | §7.3, §8.3 |
| 9 | **Inventory-completeness test**: every file/config/env in decision-bearing areas inventoried or explicitly excluded | §6.2 |
| 10 | **One content digest per (strategy_id, version)**; reusing a version for different content **fails** | §7.5 |
| 11 | **Event sequence numbers**, **per-artifact advisory locking**, **concurrent transition tests** | §7.6 |
| 12 | Rollback: **immutable evidence never auto-dropped**; downgrade refuses when populated / preserves dormant tables | §11 |
| 13 | Split into review gates **P1A / P1B / P1C / P1D** | §13 |

---

## 0. Baseline & branch (already done to host this doc; no code)

- Annotated tag `p0.1-baseline` at `54c55a8` (P0/P0.1 immutable base). Branch `p1-strategy-artifact` from it.
- **New dependency (implementation-time, P1B):** `cryptography` pinned exactly (first crypto dep; Ed25519). Its resolved version folds into **deployment identity** (§5), not the content digest of the strategy.

---

## 1. P1 impact analysis (updated)

**Adds (all net-new):** a hardened canonical-JSON primitive (ACJSON v2); a **two-object** identity model — an *executable content manifest* + a *signed validation attestation*; artifact + evidence tables with the **first DB triggers in the tree**; Ed25519 signing with **immutable key registration + append-only key-state events**; a single decision-bearing-file registry + **completeness test**; **immutable signal/decision-run lineage**; a fail-closed runtime verifier with a **runtime-derived** eligibility assessor; a lifecycle state machine; a legacy adapter.

**Must NOT touch (golden-pinned):** `signals/xsmom/v1.py` constants (LOOKBACK 252 / SKIP 21 / TOP_N 10 / SEASONING 252); `SLEEVE_BUDGET_FRACTION=0.40`; L1–L11 + DD1/DD2/DD3 (−0.05/−0.10/−0.15); ADR-0006 stop/target; DSR (`dsr_min=0.90`, `p_max=0.05`), the grandfathered `+737.31%` / `DSR≈0.853`; `tolerance_bands`; lifecycle semantics; scope separation; promotion/demotion thresholds.

**Collision points respected:** two-plane wall (`test_boundaries.py`) — hashing/schema in `atlas/core`, dcp populates code/data/risk legs, **AI leg composed from pre-computed hashes handed in** (dcp cannot import `atlas.agents`); ONE canonical JSON (does not retro-break the three legacy golden-pinned forms); artifact tables are **separate append-only** tables the mutable `quant.strategies` row references; triggers block the **owner role** too (stronger than the audit table's grant-only protection); every timestamp from `atlas.core.clock` for `make replay` byte-identity.

---

## 2. Two-object identity model (Mandates 1, 2, 3)

P1 splits identity into two cryptographically distinct objects. **The content digest is not signed directly; the attestation is signed and binds the content digest.**

### 2.1 Object A — Executable Content Manifest → `content_digest`

- ACJSON v2 (§4) canonical bytes over the **content sections** of the manifest.
- `content_digest = sha256( DOMAIN_TAG_CONTENT_V1 || canonical_bytes )` where `DOMAIN_TAG_CONTENT_V1 = b"atlas.artifact.content.v1\x00"` (domain separation from the attestation).
- **Content-digest input INCLUDES (Mandate 2):** `artifact_schema_version`, **`strategy_id`**, **`strategy_family`**, **`strategy_version`**, plus repository identity (§5), strategy spec (§3.3), portfolio spec (§3.4), risk spec + risk-config digest (§3.5), execution spec (§3.6), data identity (§3.7, `UNAVAILABLE` markers), AI overlay identity (§3.8), validation lineage (§3.9).
- **Excluded from the content digest** (recorded on the row, or living in the attestation): **`artifact_id`** and **any other per-row random/surrogate/volatile field** (a random `uuid pk` folded into a *content-addressed* digest would make identical content produce different digests and render §7.5's "same `content_digest` ⇒ idempotent return existing" path unreachable — `artifact_id` binds only inside the signed attestation, §2.2); `content_digest` itself (never recursive); the attestation + signatures; `created_at`/`created_by`/`creation_reason`; lifecycle `state`; `authoritative_eligible` (runtime-derived, §10.2); `parent_artifact_digest`, `review_reference`, `decision_reference` (these live in the **attestation**, §2.2); `branch`/dirty-flag (traceability only). **Rule:** the content digest is a pure function of *what the system is*, never of *which row stores it*.
- The **canonical bytes are stored verbatim** (`bytea`) and hashed; the JSONB copy is query-only and never the hash source.

### 2.2 Object B — Validation Attestation (the signed object) (Mandate 3)

The attestation is a small, fixed-field ACJSON object that is **domain-separated and Ed25519-signed** — **not the manifest bytes**. Fields (exactly Mandate 3):

```
attestation = {
  artifact_id,                # uuid of the artifact row being attested
  content_digest,             # from §2.1 — binds the exact executable manifest
  parent_artifact_digest,     # nullable provenance pointer
  target_lifecycle_state,     # the state this attestation authorises (e.g. 'validated')
  validation_report_digest,   # sha256 of the exact quant.validation_reports evidence
  trial_ledger_digest,        # sha256 of the lineage-scoped trial ledger snapshot
  signer_key_id,              # which registered key
  signed_at,                  # fixed-UTC (§4), injected clock
  review_reference,
  decision_reference
}
sig = Ed25519_sign( priv, DOMAIN_TAG_ATTEST_V1 || acjson_v2(attestation) )
#     DOMAIN_TAG_ATTEST_V1 = b"atlas.artifact.attestation.v1\x00"
```

**Verification chain (all fail-closed):** recompute `content_digest` from the stored canonical bytes → must equal `attestation.content_digest`; verify `sig` over `DOMAIN_TAG_ATTEST_V1 || acjson_v2(attestation)` with the registered public key; the key's **effective state** (append-only fold, §7.3/§8.3) must be `active`, `trust_domain='production'`, within `[valid_from, valid_to)`; `target_lifecycle_state` must equal the requested transition; `validation_report_digest` / `trial_ledger_digest` must match current evidence. The domain tags make a content-context signature and an attestation-context signature **non-interchangeable**.

---

## 3. StrategyArtifact manifest schema (§1.1–1.9)

Same nine sections as the approved design, with the digest-inclusion set updated per Mandate 2. Highlights (all grounded in recon; nothing changed in the referenced code):

- **§1.1 identity:** `artifact_id`, `artifact_schema_version`, `strategy_id`, `strategy_version`, `content_digest`, `parent_artifact_digest`, `created_at`, `created_by`, `creation_reason`, `review_reference`, `decision_reference`, `environment_type`, `authoritative_eligible` (**derived at runtime**, §10.2 — the stored value is a cache, never trusted).
- **§1.2 repository identity:** see §5 (Git blob/tree + deployment identity).
- **§1.3 strategy spec:** family, universe/coverage/inclusion+exclusion, min history, liquidity, **signal formula**, **signal-return type with `price_vs_total_return` first-class** (deployed ranker uses split-adjusted *price* closes; `-tr` is a validation convention — recorded honestly), lookback/skip, ranking/tie/winner-set, position counts, weighting, rebalance/calendar, signal/decision/intended-execution timestamps, holding/entry/exit, stop/target (ADR-0006), stale-signal + missing-data + corporate-action treatment. **Deployed-vs-validated divergences captured structurally** (deployed top-5 / `market=US active stock,adr` vs validated top_n=10 / `pit-sp500` 511).
- **§1.4 portfolio spec:** sleeve/cash allocation (`SLEEVE_BUDGET_FRACTION`, ADR-0017), sizing method + max/min, sector/industry/country/currency/factor-overlap/correlation/liquidity/turnover constraints, max holdings, fractional-share + residual-cash handling, rebalance-infeasibility fallback.
- **§1.5 risk spec:** **structured rules** (L1–L11, DD1/DD2/DD3, STRESS §7 / FACTOR §12 / VOL §11 — with fail-open/closed, evaluation timing, approval-time recheck) **AND** the digest of `risk.limit_sets` version + the risk implementation files. (Both, never a single opaque "risk digest".)
- **§1.6 execution spec:** paper mode only; PaperBroker next-session-open fills (`atlas/dcp/execution/paper.py`); `CostModel` commission+slippage bps side-signed (`atlas/dcp/backtest/engine.py`); shortfall_bps; FX→AUD; India-via-US-ETF/ADR; reconciliation=break-kills; idempotency/duplicate-order protection. `paper_or_live_eligibility="paper"`. **P1 adds no live trading.**
- **§1.7 data identity:** every component recorded `availability="UNAVAILABLE"` with a `reason` and `pit_status`; **no fabricated digests**; forces `authoritative_eligible=false`.
- **§1.8 AI/human overlay:** `llm_committee_enabled` (false for the deployed xsmom signal path); agent ids/roles/provider/model id; `prompt_template_hash` = existing `sha256(constitution + "\n\n" + template)` (`runner.py:200-204`); **new output-schema digest** = sha256 over the Pydantic `model_json_schema()` of `atlas/agents/schemas/*`; temperature/sampling; veto/endorse/reject; grounding-policy version; human-approval requirement + required approver role + expiry (`acknowledged_risks`, `trading.approvals`). **Composed from pre-computed hashes handed to the dcp builder** (two-plane wall).
- **§1.9 validation lineage:** experiment-family id, **`lineage` + `lineage_count`** (ADR-0016 deflation count), `trial_ledger_digest`, related/predecessor/abandoned trial ids, backtest/walk-forward/null-model run ids, gate definitions + required thresholds + observed results, `validation_report_digest`, reviewer, timestamp, and **explicit** waiver refs (a waiver is never rendered as "passed the gate").

---

## 4. ACJSON v2 — hardened canonical JSON (Mandate 5)

`atlas/core/canonical.py`. Chosen over raw RFC 8785 to reuse the audited primitive and to **forbid floats entirely** (killing `10` vs `10.0` vs `Decimal('10.000000')`). ACJSON v2 = ACJSON v1 **plus** all of Mandate 5:

1. **Serialization:** `json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)`, UTF-8.
2. **Boolean-before-integer validation:** because `bool ⊂ int` in Python, the pre-serialization validator tests `isinstance(x, bool)` **before** `isinstance(x, int)`; booleans serialize as `true`/`false` and are never coerced to `1`/`0`. An `int`-typed field rejects a `bool` and vice-versa.
3. **Unicode NFC:** every `str` (keys and values) is `unicodedata.normalize("NFC", s)` before serialization.
4. **Fixed UTC timestamps:** the sole datetime rendering is `YYYY-MM-DDTHH:MM:SS.ffffffZ` (UTC-aware required; naive → reject; always 6-digit microseconds; always trailing `Z`, never `+00:00`).
5. **Canonical UUIDs:** lowercase, hyphenated 8-4-4-4-12; any other form rejected.
6. **POSIX relative paths:** every path is forward-slash, relative to repo root; absolute/`..`-escaping/OS-specific paths rejected.
7. **Symlink & file-mode handling:** the inventory records, per decision-bearing path, `is_symlink`, `git_mode` (`100644`/`100755`/`120000`), and (for symlinks) the POSIX target; **a symlink in a decision-bearing area fails closed unless explicitly allow-listed** (Git tree entries encode mode + symlink type, §5, so this is cross-checked, not asserted by hand).
8. **Type allow-list:** only `str`, `int`, `bool`, `None`, `list`, `dict[str→…]`; **reject** `float`, `Decimal`, `set`, `tuple`, `NaN`/`Inf`, non-`str` keys. All decimal decision values arrive as **canonical decimal strings**; counts/periods as `int`.
9. **Cross-process golden vectors:** committed fixtures (`tests/golden/acjson/*.json` + expected digests) that must hash **byte-identically across OS, Python build, and process** — asserted by a test that also runs the serializer in a **subprocess** to prove no in-process state leaks in.

---

## 5. Repository & deployment identity (Mandate 6)

Two **separate** identities, both in the content digest's repository section:

**A. Source identity (clean-repo path):**
- Hash **Git blob** contents, not on-disk bytes: per decision-bearing file, the Git blob OID (`git rev-parse :<path>` / `git cat-file`) and a `sha256` of the blob content; **`git_tree_sha`** recorded (`git write-tree` / the commit's tree OID) so the whole tree — including file modes and symlink entries — is pinned.
- `git_commit_sha`, `combined_source_digest` = sha256 over ordered `(posix_path, blob_sha, git_mode)` triples.
- **Dirty tree ⇒ not eligible:** a dirty working tree may produce only a **research-only draft** (`authoritative_eligible=false`, dirty status + changed POSIX paths recorded, visibly non-authoritative). A dirty tree can never back a `validation_candidate`/`validated` artifact.

**B. Deployment identity (separate, recorded and verified):**
- **Package identity:** `sha256` over the sorted `importlib.metadata` freeze (`name==version` for every installed distribution, including the pinned `cryptography`). This is the honest substitute for the **absent lockfile** — recorded as `package_freeze_digest` with a `has_lockfile=false` limitation note. `pyproject.toml`'s own blob sha is also recorded (weak, ranges-only — labelled as such).
- **Container identity:** OCI image digest if the process is containerized (from env / runtime); otherwise `container="not_containerized"`. Recorded and re-verified at runtime.

Source and deployment identity are **distinct legs** — a code change moves the source digest; a dependency/container change moves the deployment identity; both are verified at runtime (§10).

---

## 6. Decision-bearing inventory + completeness test (Mandate 9)

### 6.1 Single registry
`atlas/dcp/artifact/inventory.py` — one declared list of `(component, posix_paths, config, env_vars, artifact_section, reason)`; **no duplicated hardcoded lists**. Digests via ordered per-file Git-blob sha256 (mirrors `FeatureDefinition.code_sha`). Deliverable: `REVIEW_PACKAGE/P1_DECISION_BEARING_FILE_MAP.md`. **Fail-closed** when a declared file is missing, a digest can't be computed, or runtime references a decision-bearing config not in the registry.

### 6.2 Completeness test (new)
A test walks **every file, config, and env var inside designated decision-bearing AREAS** (declared globs: `atlas/dcp/signals/**`, `atlas/dcp/risk/**`, `atlas/dcp/trading/**`, `atlas/dcp/execution/**`, `atlas/dcp/backtest/**`, `atlas/agents/prompts/**`, `atlas/agents/schemas/**`, `atlas/agents/runtime/registry.py`, `seeds/*.json`, and the `ATLAS_MODEL_*` / `ATLAS_LOCAL_LLM_URL` env set) and asserts each is **either inventoried or on an explicit exclusion list with a reason**. A new uninventoried, unexcluded decision-bearing file **fails the suite** — closing the "silent decision-bearing file" hole.

---

## 7. Database design (Mandates 4, 8, 10, 11, 12)

Append-only tables in schema `quant`; raw-SQL Alembic migrations (`revision="0036" down_revision="0035"`, …). Distributed across the gates (§13).

### 7.1 Core artifact & evidence tables
- **`quant.strategy_artifacts`** — `id uuid pk`, `content_digest text NOT NULL`, `artifact_schema_version text`, `strategy_id uuid` (FK → strategies), `strategy_family/name/version text`, `parent_artifact_digest text NULL`, `canonical_bytes bytea NOT NULL` (verbatim hash source), `manifest jsonb NOT NULL` (query copy), `environment_type text`, `prev_hash text NOT NULL`, `chain_hash text NOT NULL`, `created_at timestamptz NOT NULL` (injected clock), `created_by text`. Uniqueness rule §7.5.
- **`quant.strategy_artifact_events`** — append-only lifecycle transitions; **global `seq bigserial` + per-artifact `event_index int`** (§7.6); `artifact_id`, `event_type`, `from_state`, `to_state`, `actor`, `reason`, `evidence_ref`, `created_at`, `prev_hash`, `chain_hash`.
- **`quant.strategy_artifact_attestations`** — one row per signed attestation: the attestation fields (§2.2), `attestation_canonical bytea`, `signature bytea`, `signer_key_id`, `created_at`. Append-only.
- **`quant.strategy_artifact_file_digests`** — `(artifact_id, posix_path, blob_sha, git_mode, is_symlink, symlink_target, role)`. Immutable.
- **`quant.strategy_artifact_data_snapshots`** — `(artifact_id, component, provider, dataset, version, coverage, digest NULL, completeness, pit_status, availability, reason)`. Immutable; `availability='UNAVAILABLE'` in P1.

### 7.2 Immutability (first triggers in the tree; Mandate 12 downgrade in §11)
`BEFORE UPDATE OR DELETE` triggers on every artifact/evidence/attestation/key table call `quant.raise_immutable()` → `RAISE EXCEPTION`. Blocks the **owner role** too. Lifecycle advances only by **appending events**; corrections require a **successor artifact** (new content_digest), never a mutation.

### 7.3 Signing-key registration (Mandate 8)
- **`quant.strategy_artifact_signing_keys`** — **immutable registration only**: `key_id text pk`, `public_key bytea`, `algorithm text` ('ed25519'), `trust_domain text CHECK IN ('development','production')`, `valid_from`, `valid_to`, `registered_by`, `registered_at`. **No mutable `state` column.**
- **`quant.strategy_artifact_key_events`** — **append-only key-state events**: `key_id`, `seq bigserial`, `key_event_index int`, `event_type CHECK IN ('registered','disabled','revoked','expired_marked','reinstated')`, `actor`, `reason`, `created_at`, `prev_hash`, `chain_hash`. **Effective state = fold over the append-only events** (fail-closed: absent/unknown ⇒ not active). Closes trust-anchor tampering — an owner cannot flip `revoked→active` by UPDATE; only a new (audited) event can, and production trust also cross-checks an **out-of-DB anchor set** (§8.3).

### 7.4 Decision-run lineage (Mandate 4)
- **`quant.strategy_decision_runs`** — immutable: `id uuid pk`, `artifact_id uuid` (FK), `content_digest text`, `strategy_id uuid`, `run_kind text`, `session_date date`, `created_at timestamptz` (injected clock), `prev_hash`, `chain_hash`.
- **`quant.signals`** gains `decision_run_id uuid NULL` (FK → decision_runs) **and** `artifact_content_digest text NULL`. Every signal generated **after** P1C references its decision run and carries the content digest → immutable lineage `signal → decision_run → artifact → content_digest`, replacing "mutable `strategy_id` alone". Legacy signals keep the columns NULL (`LEGACY_UNBOUND`).

### 7.5 One content digest per (strategy_id, version) (Mandate 10)
Write-once / verify-on-reuse / refuse-mismatch (the `register_feature` pattern): under the **artifact-registration advisory lock keyed on `(strategy_id, strategy_version)`** (§7.6 — a creation lock, because no `artifact_id` exists yet), if a row already exists for `(strategy_id, strategy_version)` — same `content_digest` ⇒ idempotent (return existing, no insert); **different `content_digest` ⇒ `RAISE ARTIFACT_VERSION_CONTENT_CONFLICT`**. The **physical backstop is a unique index on `(strategy_id, strategy_version)` alone** — this makes a second, differing-digest row *impossible* at the DB level even under a buggy code path; the lock exists only to make the idempotent same-digest return race-free before the insert is attempted. (A unique index on the `(strategy_id, strategy_version, content_digest)` triple would be too weak — it permits two differing digests for one version.) Consequence, stated explicitly: a genuine correction or a manifest-schema change **must bump `strategy_version`** to mint a new artifact; a version may never be re-used for different content, including a change driven solely by `artifact_schema_version` (which is inside the content digest per Mandate 2). **In P1 (governance-only) no frozen strategy is re-versioned**, so this coupling has no operational effect now; it is flagged only for future artifact-schema migrations, which must bump `strategy_version` deliberately.

### 7.6 Sequencing, locking, concurrency (Mandate 11)
- Every event table carries a **global `seq bigserial`** and a **per-parent monotonic index** (`event_index` / `key_event_index`) enforced under lock.
- **Two advisory locks.** (a) **Registration lock** for write-once *creation*, keyed on `(strategy_id, strategy_version)` because no `artifact_id` exists yet: `pg_advisory_xact_lock(hashtextextended('artifact-reg:'||strategy_id::text||':'||strategy_version, 0))`. (b) **Per-artifact lock** around every *lifecycle transition*: `pg_advisory_xact_lock(hashtextextended('artifact:'||artifact_id::text, 0))`. **Fixed global lock-acquisition order:** registration-lock → per-artifact-lock → audit-lock (762001) → trial-family lock — prevents deadlock with existing writers.
- **Concurrent-transition tests**: two sessions racing the same transition — exactly one wins, the loser fails closed; per-artifact `event_index` stays gap-free and monotonic.

---

## 8. Signing & key management + attestation threat model (Mandates 3, 8)

### 8.1 Primitive
Ed25519 via pinned `cryptography`. Private keys **never** in repo/DB/committed-.env — on disk outside the tree, `.gitignore`'d. Dev keys stamped `trust_domain='development'` and **structurally unable** to authorise a real promotion.

### 8.2 What is signed (Mandate 3)
The **domain-separated attestation** (§2.2), **not** the manifest bytes. The content is bound *into* the signature via the embedded `content_digest`; the governance context (target state, validation-report digest, trial-ledger digest, review/decision refs, key id, signed ts) is signed alongside it. Signing requires explicit actor, reason, review ref, decision ref, **expected content digest**, and **expected lifecycle state**; sign + attestation-row append are one transaction.

### 8.3 Key state = immutable registration + append-only events (Mandate 8)
Effective key state is the fold in §7.3. The **production verifier trusts only** `trust_domain='production'`, effective `state='active'`, within validity, **and** whose `key_id`/`public_key` is present in an **out-of-DB anchor set** (deployment config) — so a DB tamper alone cannot mint trust. `verify_keys` re-walks the key-event chains (analogue of `verify_chain`).

### 8.4 Fail-closed matrix (all ⇒ verification FAILURE, never skip)
unknown key; disabled/revoked/expired (per event fold); malformed/wrong-length/wrong-curve/absent signature; content-digest mismatch; canonical-bytes mismatch; target-state mismatch; non-production trust domain on the authoritative path. `unsigned ⇒ cannot be validated`. Code shape is `if not verified: reject`, never `if sig: verify()`.

### 8.5 Threats & mitigations
sign-over-wrong-bytes → single ACJSON v2 + domain tags; **sign the attestation's canonical bytes (`attestation_canonical`, §7.1), never the manifest bytes** — the manifest is bound only via the `content_digest` embedded in that attestation (§2.2); dev-key-on-prod → hard trust-domain split + out-of-DB anchor; **trust-anchor tampering** → append-only hash-chained key events + config anchor; unsigned-as-skip → absence ⇒ reject; retroactive minting → legacy stays `LEGACY_UNBOUND` permanently; unpinned crypto dep → exact pin folded into deployment identity; **revocation propagation** → revocation blocks *new* validations and flags existing artifacts on the next `verify_keys` pass (no silent retroactive invalidation). **CLI** (`atlas/tools/artifact_keys.py`, `artifact_sign.py`): `keygen-dev`, `register-key`, `sign`, `verify`, `disable-key`, `revoke-key`.

---

## 9. Lifecycle state machine

`draft → validation_candidate → validated`, plus terminal `rejected`, `revoked`. Append-only history; corrections require a successor artifact.

- **draft** — may be incomplete; always non-authoritative; research-shadow execution only.
- **validation_candidate** — clean repo (Git-blob/tree, §5), complete file inventory + config identity, **mandatory data identities present**, trial-lineage identity, content digest, valid attestation-request metadata, no unresolved mandatory field. Still non-authoritative. **In P1: blocked for all real strategies (data `UNAVAILABLE`).**
- **validated** — completed validation evidence, all mandatory gates explicitly evaluated, **no failed mandatory gate** (no `validated_with_exception` introduced), a valid **attestation** by an authorised **active production** key, an append-only validation event, and matching `validation_report_digest`. Subsumes `require_signed_validation_artifact()` **verbatim** (latest `verdict=='approve'`, `report.created_at > shadowed_at`, matching `_identity`, **no override**, via `classify()`).
- **rejected** — cannot become validated; needs a successor.
- **revoked** — immutable, historically visible, cannot execute authoritatively; records actor/reason/evidence/review-ref/timestamp.

---

## 10. Runtime verification, eligibility & lineage (Mandates 4, 7)

### 10.1 Verifier (`atlas/dcp/artifact/verify.py`)
The **17-step** check before any **authoritative** operation, enumerated: (1) load the referenced artifact; (2) recompute + verify the `content_digest`; (3) verify canonical-manifest consistency; (4) verify the **attestation** signature; (5) verify signing-key effective-state; (6) verify artifact lifecycle state; (7) recompute current decision-bearing Git-blob file digests; (8) recompute relevant configuration digests (incl. **deployment identity**, §5); (9) compare runtime strategy id + version; (10) compare universe config; (11) compare portfolio config; (12) compare risk config; (13) compare execution config; (14) compare AI config (when enabled); (15) verify mandatory data-snapshot identity present; (16) verify trial-lineage identity present; (17) **fail closed on any mismatch**. **Structured errors** (never generic): `ARTIFACT_NOT_FOUND`, `ARTIFACT_DIGEST_MISMATCH`, `ATTESTATION_SIGNATURE_INVALID`, `SIGNER_KEY_UNKNOWN/DISABLED/REVOKED/EXPIRED`, `ARTIFACT_NOT_VALIDATED`, `SOURCE_FINGERPRINT_MISMATCH`, `CONFIGURATION_FINGERPRINT_MISMATCH`, `DEPLOYMENT_IDENTITY_MISMATCH`, `STRATEGY_IDENTITY_MISMATCH`, `DATA_IDENTITY_MISSING`, `TRIAL_LINEAGE_MISSING`, `AI_POLICY_MISMATCH`, `EXECUTION_POLICY_MISMATCH`, `ARTIFACT_VERSION_CONTENT_CONFLICT`.

### 10.2 `authoritative_eligible` = runtime-derived assessment (Mandate 7)
An **assessor** re-derives eligibility at runtime and returns a **structured result with explicit blocking reason codes** — the stored boolean is a **cache and is never trusted**. Reason codes: `DATA_IDENTITY_UNAVAILABLE`, `DIRTY_TREE`, `SOURCE_MISMATCH`, `DEPLOYMENT_MISMATCH`, `LINEAGE_INCOMPLETE`, `KEY_NOT_PRODUCTION`, `ATTESTATION_INVALID`, `NOT_VALIDATED`. **In P1 the assessor returns `eligible=false` for every strategy** with at least `DATA_IDENTITY_UNAVAILABLE`.

### 10.3 Decision-run lineage at runtime (Mandate 4)
Research-shadow execution may reference a `draft` artifact via a `strategy_decision_run`; every generated signal stamps `decision_run_id` + `artifact_content_digest` (§7.4). All mismatches recorded; never authoritative; reports show artifact state + runtime-match. **In P1 the authoritative path fail-closes for every strategy** (none eligible) — correct.

---

## 11. Migration & rollback (Mandate 12)

- **Additive migrations** across gates (§13): `0036`+ create tables + triggers + grants; a later migration adds `quant.signals.decision_run_id`/`artifact_content_digest` and `quant.trial_registry.artifact_id` (all nullable — no destructive change).
- **Rollback never auto-drops immutable evidence.** Each `downgrade()` first counts rows in the immutable artifact/evidence/attestation/key tables; **if any are populated it `RAISE`s** ("refusing to drop populated immutable evidence — preserve or archive manually"). The alternative documented mode: leave the tables **dormant** (drop only new constraints/triggers, keep data). Empty tables may be dropped. This protects append-only evidence from a careless `alembic downgrade`. Never edit an applied migration.

---

## 12. Backward-compatibility / legacy

Pre-P1 strategies/reports/backtests → **`LEGACY_UNBOUND`**, non-authoritative, not promotable on legacy evidence alone. **No historical rewrite; no manufactured digests.** Legacy adapter (`atlas/dcp/artifact/legacy.py`) exposes `{strategy_code_sha, version, spec_hash, legacy_report_id, reason}` without minting an artifact. The P0 interim contract stays honest and **unchanged** at `attribution.py:634-635` / `portfolio.py:156-158`:

```json
{ "artifact_digest": null, "artifact_status": "LEGACY_UNBOUND", "strategy_code_sha": "..." }
```

Since nothing reaches `validated` in P1, the served contract remains `LEGACY_UNBOUND` — truthfully.

---

## 13. Review gates & commit sequence (Mandate 13)

Four gates, each independently reviewable; **no production code or migration until this revised design is approved.**

### P1A — Canonical identity & storage
1. `atlas/core/canonical.py` — **ACJSON v2** (§4) + validator + **cross-process golden vectors**.
2. `atlas/dcp/artifact/manifest.py` — 9-section manifest + **content digest** (§2.1, digest-excludes-self, includes strategy id/family/version/schema per Mandate 2) + determinism tests.
3. **Migration 0036** — `strategy_artifacts` + `_events` (seq + event_index) + `_file_digests` + `_data_snapshots` + immutability triggers + grants; `atlas/dcp/artifact/repo.py` atomic create + hash chain + **uniqueness rule** (§7.5); immutability + concurrency (§7.6) tests; **rollback-refuse** (§11).

### P1B — Signatures & lifecycle
4. `atlas/core/signing.py` — Ed25519; **attestation** builder (§2.2) + domain separation; CLI keygen/register/sign/verify.
5. **Migration 0037** — `_attestations` + `_signing_keys` (immutable registration) + `_key_events` (append-only, Mandate 8) + triggers; key-state fold + `verify_keys`.
6. Lifecycle state machine (§9) subsuming `require_signed_validation_artifact`; per-artifact advisory locking + **concurrent-transition tests** (Mandate 11).

### P1C — Runtime verification & decision lineage
7. `atlas/dcp/artifact/inventory.py` + `REVIEW_PACKAGE/P1_DECISION_BEARING_FILE_MAP.md` + **completeness test** (Mandate 9).
8. `atlas/dcp/artifact/verify.py` — 17-step verifier + error taxonomy (§10.1); **runtime `authoritative_eligible` assessor** with blocking reasons (Mandate 7).
9. **Migration 0038** — `quant.strategy_decision_runs` + `quant.signals.decision_run_id`/`artifact_content_digest` (Mandate 4); signal-lineage stamping wired for research-shadow only; repository/deployment identity (§5) capture.

### P1D — Draft artifacts, reporting & evidence
10. Draft-artifact creation for research-shadow; legacy adapter + **honest-contract regression** (attribution/portfolio still emit `null`/`LEGACY_UNBOUND`; no historical rewrite).
11. **Evidence binding** (present fields → **migration 0039**): `quant.trial_registry.artifact_id` + audit-event payload stamping (jsonb, no migration) + validation-report references. **Deferred:** per-row artifact columns on `trading.*` (keep the transitive `signal_ids[]→signals→strategy` join) — avoids a large unrelated schema expansion.
12. Docs: **ADR-0019** (P1 artifact), README/CLAUDE.md checklist updates, finalize the file map.

---

## 14. Reserved invariants (P1 must not touch) & the honest conclusion

- No change to strategy math/params/allocation/limits/stops/targets, historical results, DSR, PIT datasets, transaction-cost assumptions, P0 lifecycle semantics, authoritative-vs-shadow separation, or promotion/demotion thresholds.
- **No strategy promoted. No data-plane remediation. No fabricated data digests.**
- **Data identities remain `UNAVAILABLE`; all real strategies remain `authoritative_eligible=false`.** P1 delivers the full machinery so that the *first* time a real content data snapshot exists (a future data-plane phase), a strategy can be honestly bound, attested, and verified — and until then, the system fail-closes and tells the truth.
