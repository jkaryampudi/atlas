# DOCUMENTATION_CODE_DISCREPANCIES — where the words disagree with the code

> Anchored to `REPOSITORY_SNAPSHOT.md` (commit `2ba38c0`, review `2026-07-20T08:41:38Z`) and
> `EVIDENCE_BASE.md` (EV-## IDs + the 7-tag taxonomy: VERIFIED / INFERRED / CLAIMED / NOT TESTED /
> NOT FOUND / PLANNED / UNKNOWN). Produced read-only; **no application code, test, config, infra, or
> dataset was modified** — only this file was written.
>
> **Purpose.** Every place where the documentation (README, CLAUDE.md, `docs/architecture/`,
> `docs/adr/`, and the review-package deep-dives `00`–`20`) disagrees with what the code actually does
> at this commit. **Governing rule (from `EVIDENCE_BASE.md`): code existence is not verified behaviour.**
> Every row below cites *both* the documented line *and* the code line; a discrepancy is listed only if
> both were read. Where a doc is accurate, it is recorded as accurate (§3) rather than manufactured into
> a finding. This is an exposure pass, not a production/investment verdict.

## 0. Headline

A deliberate, adversarial hunt across the whole doc set found **7 real documentation⇄code discrepancies**
and **6 candidate areas that turned out to be accurate** (no discrepancy). None is a benchmark-integrity
or deployed-signal-≠-validated-signal defect — those are handled in `PERFORMANCE_EVIDENCE.md` /
`STRATEGY_IMPLEMENTATION_TRACEABILITY.md`. The material pattern here is narrower and worth stating plainly:

- **The `docs/architecture/` P0 design set (docs `01`–`08`) describes an *intended enterprise
  topology* — session-token auth with scoped authZ, a Redis-Streams event bus, a Streamlit dashboard,
  and separate worker/scheduler/dashboard services — that the built system does not implement.** The
  code is deliberately simpler (no auth, Postgres-only event record, a single-file `console.html`, an
  in-process scheduler in the API). The review-package deep-dive `02_SYSTEM_ARCHITECTURE.md` **already
  catalogs each of these** in its "what we built | what a typical fund/design assumes" table and is
  therefore accurate; the discrepancies are between the *older design docs* and the code, not within
  the review package.
- The two operator-facing files (`CLAUDE.md`, `README.md`) are mostly current, with one internal
  inconsistency (backups marked DONE while the same file says the mechanism is dead) and one desk-role
  count that overstates the active LLM org.

No discrepancy below could, on its own, mislead an *investment or risk* decision (the paper API is
localhost/Tailscale-bound and the risk wall is code-enforced, EV-03/05/09), so severities top out at
**Medium**. D-02 (auth) borders High for a *security* reviewer and is called out as such.

## 1. Discrepancy table

| ID | Documented claim (source) | Actual code behaviour (evidence) | Evidence | Severity | Required correction |
|---|---|---|---|---|---|
| **D-01** | **"Anthropic SDK" wording.** The one project-doc reference implying an SDK: import-boundary rule "only `agents/runtime` may import **the LLM SDK**" (`docs/architecture/07-repository-structure.md:77`). *(The review-package docs are correct — see the fairness note in the cell to the right and §3-E.)* | There is **no Anthropic SDK**; the `anthropic` package is **NOT installed** (`REPOSITORY_SNAPSHOT.md §2`). The LLM transport is a ~13-line hand-rolled `httpx` POST: `import httpx` (`atlas/agents/runtime/llm.py:7`), `class AnthropicClient` (`:53`) → `POST https://api.anthropic.com/v1/messages` (`:64`). The boundary rule's "the LLM SDK" describes an artifact that is not present. **Fair note:** every *review-package* mention is already correct — `02_SYSTEM_ARCHITECTURE.md:216` ("No Anthropic SDK… hand-rolled `AnthropicClient` over `httpx`"), `:431` (SDK sits in the "typical-fund" comparison column, not claimed for Atlas), `12_DEPENDENCIES.md:70–71/301–303` ("there is no Anthropic SDK"), `14_SECURITY.md:319–327` — so there is **no discrepancy in `00`–`20`**. | `llm.py:7`,`:53`,`:64`; `07-repository-structure.md:77`; EV-02 · **VERIFIED** (exact-line) | **Low** | In `docs/architecture/07`, change "the LLM SDK" → "the LLM transport (`agents/runtime`, raw `httpx`)". Review-package docs need no change. |
| **D-02** | **API has session-token AuthN + scoped AuthZ + per-action step-up re-auth.** `docs/architecture/06-api-design.md:13`: "AuthN: session tokens for the dashboard; scoped service tokens… AuthZ scopes: `read`, `approve_trades`, `manage_limits`, `arm_live`, `admin`. Sensitive scopes… require step-up re-authentication per action." `06:117` ("the dashboard holds no credentials beyond **the user session**"); `01-enterprise-architecture.md:144` ("approval actions via **authenticated API calls only**"). | **No authentication or authorization exists anywhere in the API.** A grep of `atlas/api/` for a security dependency (`Depends(...)` security scheme, API-key/token/Bearer/HTTPBasic check) finds none; the only `auth_method` strings are DB columns recording *who approved* (`routers/audit.py:132`, `routers/research.py:560`), not request auth. The router that would carry it says so explicitly: "§3.2's `step_up_token` / scope plumbing is **deferred to the auth phase**" (`atlas/api/routers/trading.py:14–15`). Even the deploy runbook concedes it: "The API binds to 127.0.0.1 ONLY (it has **no auth yet**); Tailscale IS the auth boundary until step-up tokens ship" (`docs/ops/deploy-local.md:19–20`). **Fair note:** `14_SECURITY.md:18/89–104` and `16_KNOWN_LIMITATIONS.md:170` state the absence bluntly — the review package is accurate; the gap is design-doc-vs-code. | `06-api-design.md:13`,`:117`; `01-…:144` vs `trading.py:14`; `deploy-local.md:19`; EV-17 · **VERIFIED** (NOT FOUND control) | **Medium** *(borders High for a security review; mitigated by localhost/Tailscale bind + honest deploy doc + package disclosure; paper-only)* | Mark the auth model in `docs/architecture/06`/`01` as **PLANNED (Phase-N)**, not present-tense design, until built; the honest status already lives in `deploy-local.md` and `14`. |
| **D-03** | **"5 roles."** `CLAUDE.md:39` ("P2 Agents: runtime + **5 roles** + 9-test red-team suite"); `README.md:46` ("P2 agents: **5 roles** + bull/bear debate"). | The **active nightly desk wires exactly 3 LLM-calling roles**: `run_debate` (import `atlas/agents/desk.py:37`, call `:122`), `run_specialists` (import `:38`, call `:141`), `committee_memo`/CIO (import `:36`, call `:145`) — plus a **deterministic** scanner (`atlas/dcp/scanner/v1.py`, imported `atlas/ops/daily.py:135`, called `:300`; commented "deterministic scanner" `:34`). The legacy LLM role functions in `atlas/agents/roles/committee.py` (`scanner_shortlist`, `research_memo`, `macro_regime`, `sector_note`) are **imported nowhere active** (only `roles/cio` is imported by `desk.py`/`live_run.py`). The review package reaches the same code-grounded count: "**~3 LLM roles (debate/specialists/CIO) + deterministic scanner**" (`02_SYSTEM_ARCHITECTURE.md:453`, `20_APPENDIX.md:148`; `01:56–57` explicitly retires the "six-role LLM desk" phrasing). | `CLAUDE.md:39`; `README.md:46` vs `desk.py:36–38`,`:122`,`:141`,`:145`; `committee.py` (unwired); `02:453` · **VERIFIED** (exact-line trace) | **Medium** *(overstates the AI org; **not** investment/risk-critical)* | In `CLAUDE.md`/`README.md`, state "**~3 LLM roles wired into the nightly desk** (debate / specialists / CIO) + a deterministic scanner; `roles/committee.py` LLM functions are legacy/unwired." |
| **D-04** | **The dashboard/approval surface is Streamlit.** `docs/architecture/01-enterprise-architecture.md:144` ("Dashboard \| Streamlit"); `06-api-design.md:113` ("## 6. Dashboard (**Streamlit**) contract"), `:117` ("swap Streamlit later"); `07-repository-structure.md:60` ("dashboard/ # **Streamlit**; pure API client"). | The real control surface is the **single-file `console.html`** served by FastAPI: "console as sole control surface on port 8001" (`CLAUDE.md:46`); "**Streamlit Overview page superseded by the single-file console** at `/console`" (`README.md:129`). The Streamlit files still on disk (`atlas/dashboard/overview.py`, `pages/1_Research.py`, `2_Quant.py`, `3_Market.py`) are unwired remnants — `02_SYSTEM_ARCHITECTURE.md:429` records them as "**Streamlit (dep declared, files are dead remnants)**." | `01:144`,`06:113`,`07:60` vs `CLAUDE.md:46`; `README.md:129`; `02:429` · **VERIFIED** (exact-line) | **Low** | Update `docs/architecture/01/06/07` to name `console.html` (served by the API) as the dashboard; note the Streamlit files are superseded remnants. |
| **D-05** | **Redis-Streams event bus + multi-service compose topology.** `01-enterprise-architecture.md:142`/`:149` ("Redis Streams for the event bus"; DCP/orchestrator publish `signal.generated`/`proposal.created`/`risk.check.completed`/`order.filled` to Redis Streams, idempotent consumers) and `06-api-design.md:101`/`:105` ("Event model (Redis Streams → consumers)", transactional outbox). `01:145`/`07:12` claim `docker-compose` runs services "api, **worker, scheduler**, **dashboard**, db, redis." | **No `import redis` exists anywhere in `atlas/`** — the event record is the Postgres audit hash chain (verified append-only, 1,885 events, EV-04). `docker-compose.yml` defines **only three services: `db`, `redis`, `api`** (no `worker`, `scheduler`, or `dashboard` service); Redis is started but unused by code. The scheduler is **in-process in the API** ("The API process IS the scheduler", `CLAUDE.md:32`; `ATLAS_INPROC_SCHEDULER=1`). Review package is accurate: `02:427` ("in-process asyncio tick loop \| separate scheduler/worker services"), `:430` ("Redis Streams (Redis configured, **unused**)"). | `01:142`,`:145`,`:149`; `06:101`,`07:12` vs `docker-compose.yml` (db/redis/api); `CLAUDE.md:32`; `02:427`,`:430`; EV-04 · **VERIFIED** (exact-line) | **Low** | Mark the Redis-Streams bus and the worker/scheduler/dashboard services as **PLANNED** in `docs/architecture/01/06/07`; describe the shipped design (Postgres-only events, in-process scheduler, 3-service compose). |
| **D-06** | **`make replay DATE=2024-07-15 → gate=green`** stated as fact (`CLAUDE.md:35`); "`make replay DATE=…` end-to-end on fixtures (gate=green, chain verified)" checked done (`README.md:127`). | **Not reproduced this pass.** `atlas/dcp/market_data/replay.py` uses `session_scope()` (`:13`,`:27`) → it **writes the development DB and the audit chain**, which is out of scope for a non-mutating evidence pass (EV-10). The claim is therefore **CLAIMED**, reproducibility-not-established here — **not** a demonstrated falsehood. | `CLAUDE.md:35`; `README.md:127` vs `replay.py:13`,`:27`; EV-10 · **CLAIMED / NOT EXECUTED** | **Low** *(reproducibility not established this pass; flagged, not refuted)* | Attach a captured replay transcript (or a read-only replay mode against a throwaway DB) so the "gate=green" claim is independently reproducible without mutating the dev chain. |
| **D-07** | **Backups are DONE.** `CLAUDE.md:63` lists — as a struck-through **~~DONE~~** deliverable (task-queue item 8) — "launchd supervision + nightly `pg_dump` (`make install-ops`)"; `README.md:70–72` says "nightly backups via the in-process scheduler." | **Internally contradicted in the same file and by the package.** `CLAUDE.md:33–35` states "launchd agents are **dead on this Mac** (TCC blocks `~/Documents`; **exit 127 since install**)"; `16_KNOWN_LIMITATIONS.md:151–155` and `02_SYSTEM_ARCHITECTURE.md:298` state **zero backups have ever run** (first ever only scheduled 2026-07-21) and there is **no verified restore drill**. Whether any backup file has actually been produced is **UNKNOWN** to a read-only pass (needs filesystem/DB state) — but the "DONE" marking is inconsistent with the same document's "dead / exit 127" admission. | `CLAUDE.md:63` vs `CLAUDE.md:33–35`; `16:151`; `02:298` · **CLAIMED (DONE) vs VERIFIED-inconsistent / behaviour UNKNOWN** | **Medium** *(a reviewer could over-trust DR posture; the package is accurate)* | In `CLAUDE.md`/`README.md`, downgrade backups from DONE to "**mechanism written, never successfully run (launchd TCC-dead); first backup 2026-07-21; restore drill unproven**," matching `16`. |

## 2. Severity rationale

Per the package rubric, **Critical/High** is reserved for discrepancies that could mislead an
*investment or risk* decision (deployed-signal ≠ validated-signal, benchmark integrity). **None of
D-01…D-07 is of that kind:** the risk wall (two-plane boundary EV-03, no-agent-numbers EV-05,
deterministic approval re-check EV-09) is code-enforced and passes at this commit, and the API is
localhost/Tailscale-bound. The two **Medium** items are:

- **D-02 (auth)** — a security *control* described in a design doc but absent in code. It borders
  **High** for a dedicated security reviewer, and is held at Medium only because (a) the surface is
  localhost/Tailscale-bound, (b) `docs/ops/deploy-local.md` and `14`/`16` disclose the absence
  honestly, and (c) it is paper-only. See `EXECUTION_SAFETY_REVIEW.md` / `REMEDIATION_BACKLOG.md`.
- **D-03 (role count)** and **D-07 (backups)** — a materially-overstated AI-org size and an
  overstated DR posture; misleading to a CTO/ops reviewer but not to an investment/risk decision.

Everything else is **Low**: design-doc drift (D-01, D-04, D-05) or an unreproduced-this-pass claim
(D-06). No discrepancy warrants Critical/High and none is asserted.

## 3. Candidate areas checked that turned out ACCURATE (no discrepancy — recorded for fairness)

The task named several suspected discrepancies. Checked against code, **these are accurate** and are
**not** listed as findings:

- **A. Graveyard "8 of 9" vs "6 of 9."** The package uses the corrected three-bucket count: `README.md`
  (package) line 65 and `01_EXECUTIVE_SUMMARY.md:24–29` say "**6 of 9**" and **explicitly retire** the
  old "8 of 9" phrasing (which had lumped the suspended + passed-but-undeployed lineages into the
  graveyard). A grep found **no live doc still asserting "8 of 9"** (`01:28` cites it only as the
  retired phrasing). **Accurate — no discrepancy.**
- **B. Test count "~1,515" vs "1,354."** `CLAUDE.md:25` now reads "**1515 passing**", matching
  `README.md:116` ("1515 tests") and the package (`13_TESTING.md:15`, `15_PERFORMANCE.md:80`). "1354"
  appears **only historically** inside `13_TESTING.md:60–62/474` (documenting that `CLAUDE.md` was
  updated 1354→1515). There is **no live cross-document inconsistency**. *(Caveat: the exact count was
  **not** re-collected this pass — the number itself is **CLAIMED**, not in the six executed EV
  commands — but the two docs agree, so there is no discrepancy to flag.)*
- **C. "100% branch coverage."** Every occurrence is correctly scoped to **`atlas/dcp/risk` only**
  (`CLAUDE.md:42/60`, `README.md:66`, `docs/architecture/07:85`, `docs/architecture/08:41`, and the
  package `07:35/575`, `13:21–22`, `16:205`). **No doc implies whole-codebase coverage.** EV-07
  independently confirms the scope (100% on `atlas.dcp.risk`; global coverage not measured).
  **Accurate — no discrepancy.**
- **D. Scanner-as-agent.** The nightly scanner is **deterministic** (`atlas/dcp/scanner/v1.py`, `scan`
  imported `ops/daily.py:135`, called `:300`; "deterministic scanner (ADR-0007)" `:34`). The package
  describes it exactly so ("deterministic scanner", `02:453`). The LLM `scanner_shortlist` in
  `roles/committee.py` is legacy/unwired (see D-03). **No doc claims the live scanner is an LLM agent.
  Accurate — no discrepancy.**
- **E. "Anthropic SDK" inside the review package (`00`–`20`).** Every mention correctly says "no SDK /
  raw `httpx`" (see D-01's fair note). **Accurate — no discrepancy in the package.**
- **F. ADR count "17 signed ADRs."** `docs/adr/` contains **exactly 17 files, `0001`–`0017`**
  (including `0012-index-core-allocation.md`, which an initial filename filter of mine wrongly dropped
  — corrected on re-listing). `README.md:5`, `00_GROUND_TRUTH.md:24`, `10_CODEBASE_OVERVIEW.md:54`,
  and package `README.md:69` all say "17." **Accurate — no discrepancy** (ADR-0012 is present, not
  missing).

## 4. Areas left UNKNOWN (cannot be established read-only — not counted as discrepancies)

- **Book/position state** ("book was 100% cash; AMD/INTC approved 2026-07-18; earliest fill the
  2026-07-20 cycle", package `README.md:68`) and **live strategy state** ('paper') require the
  development DB / a fired cycle to confirm. Per the governing rule these are **UNKNOWN** to a
  read-only pass, not asserted as either true or discrepant. The 07-15/16/17 "missed/late/skipped
  fires" and the pending 07-20 fill are themselves disclosed as unconfirmed in the package.
- **Historical performance figures** (+737.31% vs SPY TR +593.89%, p=0.000, DSR 0.995, WF 4/4) are
  **CLAIMED / reproducibility-UNKNOWN** (EV-11) and are the subject of `PERFORMANCE_EVIDENCE.md` /
  `BACKTEST_REPRODUCIBILITY.md`, not of this doc.

---

*Method: doc lines located by `grep`/`sed` and read; code lines read via `Read`/`sed`; every row cites
both. Classifications map to `EVIDENCE_BASE.md`. No code, test, config, infra, or dataset was modified;
only this file was written.*
