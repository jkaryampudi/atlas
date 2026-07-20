# 11 — Configuration Reference

**Scope.** Every configuration surface Atlas actually reads: environment variables, the
`pydantic-settings` model, the `.env` file, `docker-compose.yml`, the `Makefile`, and the
in-database configuration that behaves like config (risk limit sets, sleeve budget
fractions, LLM budget sub-caps, model pricing). This is a **paper-mode research system on a
single machine**; several documented settings are vestigial, partially honoured, or
display-only, and those are called out explicitly rather than glossed.

Ground-truth anchor: `REVIEW_PACKAGE/00_GROUND_TRUTH.md`. Where the reviewer's spec named a
variable that does not exist under that exact name, that discrepancy is flagged here — trust
the code.

> **Reviewer's spec vs. reality (read this first).** The task spec listed
> `DAILY_LLM_BUDGET_USD`, `EODHD_API_KEY`, and `ANTHROPIC_API_KEY`. **None of those bare
> names is read anywhere.** Every Atlas variable is `ATLAS_`-prefixed. The real names are
> `ATLAS_DAILY_LLM_BUDGET_USD`, `ATLAS_EODHD_API_KEY`, `ATLAS_ANTHROPIC_API_KEY`. Setting the
> unprefixed forms does nothing. This is documented per-variable below.

---

## 1. How configuration is loaded — two mechanisms, and why it matters

Atlas has **two independent config paths that do not share a source**, and the split is the
root cause of a real production incident (the Anthropic-key 401 outage noted in
`00_GROUND_TRUTH.md`).

### 1a. Path A — `pydantic-settings` (`atlas/core/config.py`)

```python
# atlas/core/config.py:5-14
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas"
    redis_url: str = "redis://localhost:6379/0"
    trading_mode: str = "paper"
    base_currency: str = "AUD"
    limit_mode: str = "small_aum"
    daily_llm_budget_usd: float = 10.0
    eodhd_api_key: str = ""
```

`get_settings()` (`atlas/core/config.py:17`) constructs a fresh `Settings()` on each call.
Precedence for these seven fields (pydantic-settings default order):

1. explicit init kwargs (none in this codebase),
2. **`os.environ`** (`ATLAS_`-prefixed),
3. **`.env` file** (`ATLAS_`-prefixed),
4. the hard-coded default above.

So for a Path-A field, a value in `.env` **is** honoured even if the shell never exported it,
and an exported shell var wins over `.env`. There is **no caching** — every `get_settings()`
re-reads the environment and `.env`, so a mid-process change is picked up (fine for a single
long-running API, but note it re-parses `.env` on every call — a minor inefficiency, not a
bug).

### 1b. Path B — raw `os.environ.get(...)` (everywhere else)

The agent runtime, ops layer, scheduler, alerts, model registry, budget sub-caps, dashboard
client, and backup script read the environment **directly**, bypassing `Settings` entirely
(examples: `atlas/agents/runtime/registry.py:48-62`, `atlas/ops/alerts.py:53`,
`atlas/api/main.py:43`, `atlas/dashboard/_client.py:9`). pydantic-settings **does not export
`.env` into `os.environ`** — it only populates the `Settings` object. Therefore **a Path-B
variable set only in `.env` is invisible** unless the process environment was populated some
other way.

**How the process environment actually gets populated:** the `Makefile` does the export
(`Makefile:2-4`):

```make
-include .env
export
```

So `make api`, `make daily`, `make replay`, `make test` all inherit the full `.env` into
`os.environ`. **A bare `uvicorn atlas.api.main:app --port 8001` launched from a shell that
never sourced `.env` gets only Path-A `.env` reads and loses every Path-B variable** — no
Anthropic key (desk silently disabled, `atlas/ops/daily.py:695`), no in-process scheduler
(`ATLAS_INPROC_SCHEDULER` unset ⇒ nightly cycle never fires, `atlas/api/main.py:43`), no
alert webhook, budget sub-caps fall back to code defaults. This is exactly the failure mode
recorded in ground truth ("a manual restart dropped it → 401s; nothing loads `.env` into
`os.environ` automatically"). **[IMPLEMENTED but fragile — this is real technical debt.]**

```mermaid
flowchart TD
    ENV[.env file<br/>plaintext, gitignored] -->|pydantic reads directly| S[Settings object<br/>7 fields]
    ENV -->|make -include + export| OS[os.environ<br/>process env]
    SH[shell export / launchd plist] --> OS
    OS -->|wins over .env| S
    OS --> B["Path B: raw os.environ.get(...)<br/>Anthropic key, scheduler, alerts,<br/>model registry, budget sub-caps,<br/>local LLM URL, API URL"]
    S --> A["Path A: get_settings()<br/>DB URL, redis, mode, currency,<br/>limit_mode, daily budget, EODHD key"]
    note[".env alone feeds Path A.<br/>Path B ALSO needs os.environ populated<br/>(make/export/launchd), or it silently<br/>falls back to defaults/empty."]
```

**Asymmetry table:**

| Reads `.env` directly (Path A) | Needs `os.environ` populated (Path B) |
|---|---|
| `ATLAS_DATABASE_URL`, `ATLAS_REDIS_URL`, `ATLAS_TRADING_MODE`, `ATLAS_BASE_CURRENCY`, `ATLAS_LIMIT_MODE`, `ATLAS_DAILY_LLM_BUDGET_USD`, `ATLAS_EODHD_API_KEY` | `ATLAS_ANTHROPIC_API_KEY`, `ATLAS_ALERT_URL`, `ATLAS_INPROC_SCHEDULER`, `ATLAS_MODEL_*`, `ATLAS_LOCAL_LLM_URL`, `ATLAS_BUDGET_{NIGHTLY,ANALYZE,SHADOW}`, `ATLAS_API_URL`, `ATLAS_BACKUP_DIR`, `ATLAS_TEST_DATABASE_URL` |

---

## 2. Master variable table

Legend: **Path** A = pydantic `Settings`; B = raw `os.environ`; sh = shell/`Makefile`/script
only. Tag = implementation status of the setting's *effect*.

| Variable | Default | Path | Read at | Effect / tag |
|---|---|---|---|---|
| `ATLAS_DATABASE_URL` | `postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas` | A + B | `config.py:8`, `tools/doctor.py:50`, `agents/evals/run.py:267`, `tests/conftest.py:21` | Primary DB DSN. **[IMPLEMENTED]** |
| `ATLAS_REDIS_URL` | `redis://localhost:6379/0` | A | `config.py:9` | **[PLACEHOLDER — dead config]**. No `import redis` exists anywhere. |
| `ATLAS_TRADING_MODE` | `paper` | A | `config.py:10`, `api/routers/system.py:15,25` | Reported by `/v1/system/health`; `armed` is hard-coded `False`. Live path **[PLANNED — NOT BUILT]**. |
| `ATLAS_BASE_CURRENCY` | `AUD` | A | `config.py:11`, `api/routers/system.py:18` | **[PARTIAL]** — display only; FX module hard-codes `BASE_CURRENCY="AUD"` (`market_data/fx.py:20`). |
| `ATLAS_LIMIT_MODE` | `small_aum` | A | `config.py:12`, `api/routers/system.py:17` | **[PARTIAL]** — display only; limit-set loader ignores mode (`risk/engine.py:140`). |
| `ATLAS_DAILY_LLM_BUDGET_USD` | `10.0` | A | `config.py:13` → `runner.py:271`, `budget.py:26` | Global daily LLM cost breaker. **[IMPLEMENTED]** |
| `ATLAS_EODHD_API_KEY` | `""` (empty ⇒ fixtures) | A | `config.py:14` → `adapters/__init__.py:25` | Market-data vendor key. Empty selects the fixture adapter. **[IMPLEMENTED]** |
| `ATLAS_ANTHROPIC_API_KEY` | `""` | B | `registry.py:62`, `shadow_compare.py:299`, `daily.py:695` | LLM desk auth + on/off gate. Unset ⇒ desk skipped. **[IMPLEMENTED]** |
| `ATLAS_ALERT_URL` | `""` (⇒ stderr) | B | `ops/alerts.py:53` (executable read; `reporting/brief.py:32` is only a docstring mention) | ntfy-style push webhook. Unset degrades to stderr; the audit event still lands. **[IMPLEMENTED]** |
| `ATLAS_INPROC_SCHEDULER` | unset (`!= "1"` ⇒ off) | B | `api/main.py:43` (executable read; `ops/scheduler.py:4` is only a docstring line) | `"1"` makes the API process the scheduler (Mac interim). **[IMPLEMENTED]** |
| `ATLAS_MODEL_DEFAULT` | `claude-sonnet-4-6` (code default) | B | `registry.py:49`, `shadow_compare.py:117` | Fallback model for every role. **[IMPLEMENTED]** |
| `ATLAS_MODEL_<ROLE>` | none ⇒ `ATLAS_MODEL_DEFAULT` | B | `registry.py:47` | Per-role model override, dynamic name. **[IMPLEMENTED]** |
| `ATLAS_LOCAL_LLM_URL` | `""` | B | `registry.py:56,78` | Base URL for `local/…` OpenAI-compatible models. Required iff a model string starts `local/`. **[IMPLEMENTED]** |
| `ATLAS_BUDGET_NIGHTLY` | `6.00` (`runner.py:169`) | B | `runner.py:193`, `brief.py:227` | Nightly-desk spend watermark (sub-cap). **[IMPLEMENTED]** |
| `ATLAS_BUDGET_ANALYZE` | `3.00` code / **`5.00` in current `.env`** | B | `runner.py:193` (env read); `ops/analyze.py:197` (`budget_surface("analyze")` binding — :193 is a comment) | Console-analyze spend watermark. **[IMPLEMENTED]** (see §5 note on the value drift). |
| `ATLAS_BUDGET_SHADOW` | `3.00` (`runner.py:170`) | B | `runner.py:193`, `shadow_compare.py:40` | Shadow model-comparison watermark. **[IMPLEMENTED]** |
| `ATLAS_API_URL` | `http://localhost:8001` | B | `dashboard/_client.py:9` (Streamlit) | Base URL the legacy Streamlit dashboard hits. **[IMPLEMENTED]** (Streamlit dashboard is largely superseded by `console.html`). |
| `ATLAS_BACKUP_DIR` | `~/AtlasBackups` | sh | `ops/backup.sh`, `ops/backup_linux.sh:9` | pg_dump destination (both the Mac and Linux backup scripts). **[IMPLEMENTED]** (but backups have never run — see §6/§7). |
| `ATLAS_BACKUP_REMOTE` | unset (⇒ no off-box copy) | sh | `ops/backup_linux.sh:24-26` | Optional `rclone` off-box copy target (e.g. `b2:atlas-backups`) run after the nightly dump — the mitigation for the "backup that does not survive the disk" risk. **Linux script only**; the Mac `ops/backup.sh` has no remote-copy path. **[IMPLEMENTED — Linux path only; inert on the Mac deployment of record]** |
| `ATLAS_MIGRATE_FORCE` | unset (⇒ refuse) | sh | `ops/migrate_from_dump.sh:15` | Dump-restore safety flag: `=1` is **required** to overwrite a DB that already holds audit events ("a mistaken re-run must not vaporise a live chain"). Absent/`!=1` ⇒ the restore drill refuses and exits 2. **[IMPLEMENTED]** |
| `ATLAS_TEST_DATABASE_URL` | derived: `…/atlas_test` from `ATLAS_DATABASE_URL` | B | `tests/conftest.py:24-33` | Test DB DSN; name **must** start `atlas_test*` or conftest raises. **[IMPLEMENTED]** |
| `VIRTUAL_ENV` | (set by venv activation) | B | `tools/doctor.py:29` | Only used by `atlas doctor` to warn if the venv is inactive. |
| `POSTGRES_DB/USER/PASSWORD` | `atlas/atlas/atlas_local_only` | compose | `docker-compose.yml` | Postgres container bootstrap. **[IMPLEMENTED]** |

---

## 3. Detailed reference

### 3.1 `ATLAS_DATABASE_URL` — the primary datastore
- **Default:** `postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas`
  (`config.py:8`, and repeated as the fallback in `tools/doctor.py:71` and
  `tests/conftest.py:22`).
- **Acceptable range:** any SQLAlchemy `postgresql+psycopg://…` DSN reaching a Postgres 16
  instance that has been migrated to head (`alembic upgrade head`). Docker compose supplies
  `postgresql+psycopg://atlas:atlas_local_only@db:5432/atlas` (container hostname `db`).
- **Impact:** every schema in the system (market, quant, trading, research, audit, learning,
  reporting, risk, fxlab, workflow) lives here; without it nothing runs. `doctor` counts
  tables and demands ≥16 before declaring migrations applied (`doctor.py:60`).
- **Dependencies:** Postgres reachable; migrations applied; the DB user owning the schemas.
- **Risk if wrong:** total outage, or — worse — **pointing tests at the real DB**. The
  test-DB guard (`conftest.py:31`) only refuses names not starting `atlas_test`; the *dev*
  URL itself is destructive if a fixture ran against it, which is precisely why fixtures use
  the separate `ATLAS_TEST_DATABASE_URL`. The **password `atlas_local_only` is a literal
  default in code, in `docker-compose.yml`, and in `doctor`'s fix hint** — see §6 security.

### 3.2 `ATLAS_REDIS_URL` — **dead configuration**
- **Default:** `redis://localhost:6379/0` (`config.py:9`).
- **Reality:** `grep -rn "import redis" atlas/` returns **nothing**. No queue, cache, lock,
  or pub/sub uses Redis. `docker-compose.yml` still starts a `redis:7` container and injects
  `ATLAS_REDIS_URL` into the `api` service, and `doctor`'s fix hint says
  `docker compose up -d db redis` — all vestigial. The in-process concurrency guard is a
  `threading.Lock` (`ops/scheduler.py:31`), not Redis.
- **Tag:** **[PLACEHOLDER]**. Setting it has no effect on application behaviour.
- **Risk if wrong:** none functionally; it is misleading documentation debt that implies a
  distributed component that does not exist.

### 3.3 `ATLAS_TRADING_MODE` — mode label, not a live switch
- **Default:** `paper` (`config.py:10`). Acceptable values in intent: `paper` | `live`.
- **Impact:** surfaced by `GET /v1/system/health` and `/v1/system/mode`
  (`api/routers/system.py:15,25`) and the dashboard "Mode" tile (`dashboard/overview.py:21`).
- **Critical caveat:** `armed` is **hard-coded `False`** in both endpoints
  (`system.py:16,25`) with the comment "live arming is a Phase 6 mechanism; always false
  until then". Setting `ATLAS_TRADING_MODE=live` changes a **string in a health readout** and
  nothing else — there is **no live broker, no arming flow, no order routing**. Live trading
  is **[PLANNED — NOT BUILT]**. (Label-drift note, trusting the code: this comment says
  "Phase 6", while CLAUDE.md/the roadmap call live trading **Phase 7** — a cosmetic doc-vs-code
  phase-number skew, not a functional discrepancy.)
- **Risk if wrong:** cosmetic only today; dangerous *later* if a live path is built without
  re-checking that this flag alone must never enable capital deployment.

### 3.4 `ATLAS_BASE_CURRENCY` — **partially honoured**
- **Default:** `AUD` (`config.py:11`).
- **Impact claimed:** base currency for the hypothetical A$100k book.
- **Reality:** the FX ingestion/translation module **hard-codes** `BASE_CURRENCY = "AUD"`
  as a module constant (`market_data/fx.py:20`, used at `fx.py:33`), **not** read from
  `get_settings()`. So `ATLAS_BASE_CURRENCY=USD` would change the health-readout string
  (`system.py:18`) while FX conversion, `fx_to_aud`, and risk rule L11 continue to translate
  to AUD. **[PARTIAL]** — do not rely on this env var to re-base the book.
- **Risk if wrong:** silent inconsistency between the reported base currency and the actual
  translation base. For a single-Principal AUD fund this is latent; it becomes a real bug the
  moment anyone trusts the env var.

### 3.5 `ATLAS_LIMIT_MODE` — **display-only**
- **Default:** `small_aum` (`config.py:12`, ADR-0001 decision 2).
- **Impact claimed:** selects the risk limit regime.
- **Reality:** the active limit set is chosen purely by **version and effective date**, not
  by mode: `load_active_limit_set` runs `SELECT … FROM risk.limit_sets WHERE effective_from
  <= :on ORDER BY version DESC LIMIT 1` (`risk/engine.py:140-144`) — **no `WHERE mode`
  clause**. The `mode` column exists in `risk.limit_sets` (and both seeded sets carry
  `mode="small_aum"`), but changing `ATLAS_LIMIT_MODE` would **not** switch limit sets. It is
  echoed only in `/v1/system/health` and the dashboard tile. **[PARTIAL / display-only]**.
- **Risk if wrong:** none today (only one mode exists); a reviewer should note that a second
  mode could not be activated by this variable as currently wired.

### 3.6 `ATLAS_DAILY_LLM_BUDGET_USD` — the global cost breaker
- **Default:** `10.0` (`config.py:13`). Range: any positive float; the ground-truth global
  cap is $10/day (Constitution 5.4).
- **Mechanism:** `spend_and_check` (`agents/runtime/budget.py:14-29`) sums today's
  `research.agent_runs.cost_usd` and raises `BudgetExhausted` if the *new* total would exceed
  the cap. Checked **first among the two breakers** in `run_agent` (`runner.py:270-271`) — the
  global cap is evaluated before the surface watermark and always binds; a surface sub-cap can
  only be stricter, never looser (`runner.py:146-149`, precedence comment).
- **Timing — this is a *post-completion* breaker, not a fuse.** The check at `runner.py:270`
  runs **after** the LLM completion already returned (`_complete_with_backoff`,
  `runner.py:248`), so the in-flight call's tokens are always billed *before* the breaker can
  fire. It is a between-calls gate that can only block the **next** run — it cannot cap a
  single runaway call mid-stream. The only true backstop against a single oversized call is the
  vendor account itself (see the risk note below).
- **"Day" boundary is DB-clock, not injectable clock.** `run_agent` calls `spend_and_check`
  without the `day` argument, so the daily window is `created_at::date = CURRENT_DATE`
  (`budget.py:17-20`) — the **Postgres server clock/timezone**, *not* `atlas.core.clock`
  (CLAUDE.md invariant 6). Deterministic-replay and frozen-clock tests do not move this
  reset; the cost breaker's day rolls over on DB wall-clock midnight.
- **Dependencies:** cost is derived from token counts × the reviewed pricing table (§5.2);
  the breaker is only as honest as that table.
- **Risk if wrong:** set too high ⇒ a runaway desk could spend real Anthropic dollars beyond
  intent (bounded only by the vendor account); set to `0` or negative ⇒ **every** LLM run is
  killed (`budget_kill` rows) and the desk is effectively disabled. It is a DB-counter
  breaker with **no hard vendor-side spend cap behind it** — the only backstop is Anthropic's
  own account limits.

### 3.7 `ATLAS_EODHD_API_KEY` — the single market-data vendor key
- **Default:** `""` ⇒ deterministic **fixture adapter** (`adapters/__init__.py:25`,
  `config.py:14`).
- **Mechanism:** `adapter_from_settings` reads `settings.eodhd_api_key` (Path A, so `.env`
  suffices) and returns `EodhdAdapter(key, …)` when non-empty, else `FixtureAdapter`. Several
  tools additionally hard-require it and exit if missing (`index_membership.py:251`,
  `tools/backfill_gics.py:227`, `fxlab/ingest.py:137` — the FX sandbox has **no** fixture
  fallback).
- **Dependencies:** a paid EODHD "All-In-One" subscription (~$99/mo); it is the **only**
  vendor (single-vendor lock-in, ground truth §Data plane). No PIT fundamentals from this
  vendor (the biggest research blocker).
- **Risk if wrong:** wrong/expired key ⇒ vendor 401/403 during ingest; empty key ⇒ the whole
  system silently runs on fixtures (safe for dev, catastrophic if mistaken for real data in a
  backfill). Note the error strings across the codebase reference the env var name
  `ATLAS_EODHD_API_KEY`, but the value is consumed via `Settings`.

### 3.8 `ATLAS_ANTHROPIC_API_KEY` — LLM auth **and** desk on/off gate
- **Default:** `""` (read at `registry.py:62`, `shadow_compare.py:299`).
- **Dual role:** (1) auth for `AnthropicClient`; (2) an **on/off gate** — the daily cycle
  only wires the desk if the key is present: `if os.environ.get("ATLAS_ANTHROPIC_API_KEY"):`
  (`ops/daily.py:695`). Unset ⇒ the nightly desk is skipped entirely (the red-team suite
  still exercises the cage without it — `dashboard/pages/1_Research.py:22`).
- **Path B pitfall:** it is **not** a `Settings` field, so `.env` alone does not surface it
  to `os.environ.get(...)` — the incident in §1b. `shadow_compare.py:301` raises a clear
  `ValueError("ATLAS_ANTHROPIC_API_KEY is not set …")` when a comparison is attempted without
  it.
- **Risk if wrong:** a **wrong/dropped key ⇒ 401 is NON-transient** and is **not** retried.
  `_is_transient` (`runner.py:112-119`) is True only for a timeout / HTTP 429 / 5xx; a 4xx
  besides 429 "is a configuration bug and propagates raw" (`runner.py:86`). `run_desk` catches
  only `AgentRunFailed` / `TransientLlmFailure` / `BudgetExhausted` / `LookupError` per symbol
  (`desk.py:118-157`), **not** a raw `httpx.HTTPStatusError` — so a 401 aborts the **entire
  desk** (every symbol, not a per-symbol skip). It surfaces one level up in the T7 step
  (`daily.py:431`), which marks `desk_failed`, returns `"desk FAILED"`, and fires the
  **once-daily billing-outage page** (`maybe_billing_outage_alert`, `daily.py:434-441`, whose
  own comment — "Four silent billing outages in five days are why this exists" — is exactly
  this scenario). unset ⇒ desk silently off (no memos, no proposals from the reasoning plane;
  the on/off gate at `daily.py:695`). This is a plaintext secret (§6).

### 3.9 Model registry — `ATLAS_MODEL_DEFAULT` and `ATLAS_MODEL_<ROLE>`
- **Resolution** (`registry.py:46-50`): `ATLAS_MODEL_{ROLE.upper().replace('-','_')}` →
  `ATLAS_MODEL_DEFAULT` → built-in `DEFAULT_MODEL = "claude-sonnet-4-6"` (`registry.py:31`).
- **Role names** (evidenced in code/comments): `cio`, `scanner`, `debate_bull`,
  `debate_bear` (`roles/debate.py:72` → `ATLAS_MODEL_DEBATE_BEAR`), `quality_analyst`,
  `growth_analyst`, `macro_analyst` (`roles/specialists.py:116` →
  `ATLAS_MODEL_QUALITY_ANALYST` / `_GROWTH_ANALYST` / `_MACRO_ANALYST`). Any role string is
  accepted; the env-var name is derived, so an unset override falls through to the default.
- **`local/` routing:** a resolved model string beginning `local/` routes to
  `OpenAICompatClient` at `ATLAS_LOCAL_LLM_URL` (§3.10) and is priced at $0 (§5.2).
- **Client caching:** one client per `(role, model, api_key, local_url)` for the process
  (`registry.py:80-87`) to fix a real connection-pool leak (~45 leaked httpx pools per
  ~15-symbol cycle, `registry.py:9-20`). Injected `http_client` (tests) bypasses the cache.
- **Impact/risk:** picking `claude-opus-4-8` for every role (5× the input price of Sonnet,
  §5.2) can blow the daily budget quickly; an unpriced model string fails **closed** at the
  highest legacy rate (§5.2), so mis-typing a model over-counts spend (safe) rather than
  under-counting (unsafe). Model changes are meant to go through shadow mode first, then a
  Principal registry decision (`shadow_compare.py:5`, Constitution 7.2).

### 3.10 `ATLAS_LOCAL_LLM_URL` — LAN inference endpoint
- **Default:** `""`. **Required iff** a resolved model string starts `local/`
  (`registry.py:56-59` raises `ValueError` otherwise).
- **Impact:** routes to an OpenAI-compatible server on the LAN; API spend is billed at $0
  (electricity, not dollars — `runner.py:48-50`, `MODEL_RATES_PER_MTOK` local prefix
  `(0.0, 0.0)`). Tokens are still recorded; the route is re-prefixed `local/` before pricing
  so LAN inference can never be billed as vendor spend (`runner.py:251-258`).
- **Risk if wrong:** unreachable URL ⇒ the completion call fails; a `local/` model with an
  unset URL raises before any client is cached.

### 3.11 Budget sub-caps — `ATLAS_BUDGET_NIGHTLY` / `_ANALYZE` / `_SHADOW`
- **Defaults** (`runner.py:169-170`, `SURFACE_BUDGET_DEFAULTS_USD`): nightly **$6.00**,
  analyze **$3.00**, shadow **$3.00**. Overridable per-surface via the matching env var
  (`surface_cap_usd`, `runner.py:189-196`).
- **Semantics:** a sub-cap is a **one-directional watermark on the shared daily tally**, not
  a per-surface meter (`research.agent_runs` has no `surface` column — deliberately out of
  scope, `runner.py:150-159`). A surface may not push the day's *total* spend past its
  watermark. This guarantees the nightly desk always finds headroom regardless of how many
  console-analyze clicks preceded it; the reverse (a heavy nightly starving later analyze) is
  intentional — the nightly desk is the priority surface.
- **Binding sites:** `run_desk` defaults to `nightly`; `ops/analyze.py:197` wraps its desk
  call in `budget_surface("analyze")`; `shadow_compare` binds `shadow`. Entry points that
  bind no surface (e.g. `live_run`) answer to the global breaker alone (`runner.py:161-168`).
- **Failure mode by design:** `surface_cap_usd` does `SURFACE_BUDGET_DEFAULTS_USD[surface]`
  with **no `.get` fallback** (`runner.py:196`) — an unknown surface with no env var raises
  `KeyError` deliberately, so an unbudgeted surface can never silently inherit the $10 global
  cap.
- **Value drift to flag:** the code default for analyze is **$3.00**, but the live `.env`
  sets `ATLAS_BUDGET_ANALYZE` (per its own comment, "raised from $3 → $5 by Jay 2026-07-19";
  CLAUDE.md/ground-truth also say $5). **The operational value ($5) differs from the code
  default ($3) and from the reporting default the morning brief prints
  (`NIGHTLY_WATERMARK_DEFAULT_USD=6.00`, `brief.py:64`).** A reviewer should treat the
  effective analyze watermark as **whatever `.env` currently holds**, not the code default.
- **Risk if wrong:** a sub-cap set **above** the global $10 does nothing (global always
  wins); a sub-cap set very low starves that surface (e.g. `ATLAS_BUDGET_NIGHTLY=0` ⇒ the
  nightly desk cannot spend and every desk run kills at the first token).

### 3.12 `ATLAS_ALERT_URL` — best-effort push webhook
- **Default:** `""` (`ops/alerts.py:53`). Intended value: an ntfy topic, e.g.
  `https://ntfy.sh/<your-private-topic>` (`alerts.py:4`).
- **Behaviour:** `notify()` (`alerts.py:50-68`) is best-effort and **never raises**; unset
  ⇒ the alert prints to stderr with a `(ATLAS_ALERT_URL unset — stderr only)` suffix. The
  durable record is the **audit event**, which lands regardless (`alert_urgent`,
  `alerts.py:71-77`) — "unset transport must never mean an unrecorded condition."
- **Risk if wrong:** unset (the common state, per ground truth) ⇒ **no out-of-band paging**;
  a failing backup or a broken reconciliation is only visible in stderr/logs and the audit
  chain, not on the operator's phone. A wrong URL ⇒ silent non-delivery (2xx check only).
  This is the *entire* alerting layer — there is no PagerDuty/Sentry/Prometheus.

### 3.13 `ATLAS_INPROC_SCHEDULER` — the interim scheduler switch
- **Default:** unset (treated as off; only the exact string `"1"` enables it,
  `api/main.py:43`).
- **Behaviour:** when `"1"`, the FastAPI lifespan spawns `scheduler_loop()` which fires the
  **T0–T9 cycle at 23:30 UTC** and the **pg_dump backup at 00:30 UTC** (`ops/scheduler.py:9,
  27-28`). This is the **Mac interim** because launchd is TCC-blocked on `~/Documents`
  (ground truth §Ops). On a Linux box, systemd timers own the schedule and this stays **unset**
  — the docstring warns "never both, or the day fires twice" (idempotent but noisy,
  `scheduler.py:5-7`).
- **Path B pitfall:** it's a raw `os.environ` read, so launching `uvicorn` without exporting
  `.env` leaves the scheduler **off** and nothing runs the nightly cycle. It is present in the
  current `.env` (so `make api` enables it).
- **Risk if wrong:** unset in production-of-record ⇒ **no autonomous daily cycle and no
  backups** (this is precisely why the fund has taken zero backups to date — see §6); set on
  *both* the Mac and a future Linux box ⇒ double-fire (idempotent by per-day `run_id`, so
  harmless but confusing).

### 3.14 `ATLAS_API_URL` — dashboard client base URL
- **Default:** `http://localhost:8001` (`dashboard/_client.py:9`; also set in the `make
  dashboard` target).
- **Impact:** base URL the **Streamlit** dashboard (`atlas/dashboard/*`) hits as a pure API
  client (5s timeout, degrades per-panel). Note the primary UI is the single-file
  `console.html` served same-origin by the API itself (`api/main.py:60-62`); the Streamlit
  dashboard is a secondary/legacy surface.
- **Risk if wrong:** the Streamlit panels show connection errors; no effect on the core
  system.

### 3.15 `ATLAS_BACKUP_DIR` / `ATLAS_BACKUP_REMOTE` — pg_dump destination (shell only)
- **Default:** `ATLAS_BACKUP_DIR` = `~/AtlasBackups` (`ops/backup.sh`, `ops/backup_linux.sh:9`,
  `Makefile:31`). `ATLAS_BACKUP_REMOTE` = unset.
- **Behaviour:** `backup.sh` sources `.env` itself (`set -a && source .env`), then
  `docker exec atlas-db-1 pg_dump … | gzip > $DEST/atlas-<date>.sql.gz`, keeps 30 days, and
  alerts (high priority) if the dump is `<10240` bytes or pg_dump exits non-zero.
- **Off-box copy — Linux only:** the *Linux* script `ops/backup_linux.sh:24-26` adds an
  optional `rclone copy "$FILE" "$ATLAS_BACKUP_REMOTE/"` step (e.g. `b2:atlas-backups`,
  `s3:my-bucket/atlas`) after the local dump, and fails loudly if `rclone` is missing or the
  copy errors. **The Mac `ops/backup.sh` has no equivalent** — so on the deployment of record
  (the Mac, §7) there is *no* off-box copy mechanism at all, wired or unwired.
- **Risk if wrong:** with `ATLAS_BACKUP_REMOTE` unset (or on the Mac, where it does nothing),
  backups sit on the same disk as the DB ⇒ an on-disk backup that does not survive the disk
  (the script's own comment warns about this). See §6/§7.

### 3.16 `ATLAS_TEST_DATABASE_URL` — test isolation
- **Default:** derived — `ATLAS_DATABASE_URL` with the DB name replaced by `atlas_test`
  (`tests/conftest.py:23-25`).
- **Hard guard:** the connected DB name **must** start with `atlas_test`, else conftest
  raises at import (`conftest.py:31-33`). This lets parallel workstreams use
  `…/atlas_test_<name>` while making it structurally impossible to point destructive
  TRUNCATE fixtures at the dev DB (`conftest.py:26-29`, `integration/conftest.py`).
- **Risk if wrong:** the guard converts the classic "tests wiped my dev database" footgun
  into an import-time error — a genuine strength. Misconfiguring it fails loudly, not
  silently.

### 3.17 `ATLAS_MIGRATE_FORCE` — dump-restore overwrite guard (shell only)
- **Default:** unset (⇒ refuse). Read only by `ops/migrate_from_dump.sh:15`.
- **Behaviour:** the restore-drill script (`migrate_from_dump.sh` — restore a pg_dump into this
  box's Postgres, then `verify_chain` end-to-end) first counts `audit.decision_events`; if the
  target DB already holds any events it **refuses and exits 2** unless `ATLAS_MIGRATE_FORCE=1`
  is set — "a mistaken re-run must not vaporise a live chain" (`migrate_from_dump.sh:6-7,15-18`).
- **Risk if wrong:** setting `=1` against a box whose chain is *not* disposable drops and
  recreates the `atlas` database (`migrate_from_dump.sh:22-23`), destroying the append-only
  audit hash-chain (CLAUDE.md invariant 4). This is the one shell flag that can deliberately
  bypass the destructive-DB footgun protections §3.1/§3.16 otherwise enforce — the guard is the
  *default*, the flag is the escape hatch.

### 3.18 Non-`ATLAS_` variables

| Variable | Where | Notes |
|---|---|---|
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `docker-compose.yml` | Bootstrap the `postgres:16` container as `atlas/atlas/atlas_local_only`. **Password is a literal.** |
| `VIRTUAL_ENV` | `tools/doctor.py:29` | Only used to warn the venv is inactive. |
| `DATE` (make var) | `Makefile:17` | `make replay DATE=2026-07-10` — deterministic fixture replay, not an env var. |

---

## 4. In-database configuration (behaves like config, versioned, ADR-gated)

Several things a reviewer would expect as "config" are **deliberately not env vars** — they
are versioned rows or code constants that changing requires a signed ADR. This is a design
choice (Constitution: "config that moves capital is governed, not an env flag").

### 4.1 Risk limit sets — `risk.limit_sets` (DB, versioned, dual-confirm)

The active limit set is loaded by **version + effective date only** (`risk/engine.py:140`,
§3.5). Two sets are seeded:

**`limit_set_v1`** (`seeds/limit_set_v1.json`, ADR-0001, `mode="small_aum"`):

| Key | Value | Meaning |
|---|---|---|
| `L1_max_stock_weight` | `0.08` | max single stock 8% |
| `L2_max_etf_weight` | `0.15` | max ETF 15% |
| `L3_max_sector_exposure` | `0.25` | max sector 25% |
| `L4_max_india_sleeve` | `0.30` | India sleeve 30% |
| `L5_min_cash_reserve` | `0.20` | min cash 20% |
| `L6_max_risk_per_trade` | `0.01` | risk/trade 1% |
| `L7_max_aggregate_open_risk` | `0.06` | aggregate open risk 6% |
| `L8_corr_threshold` / `L8_corr_combined_weight` | `0.8` / `0.12` | correlation threshold / combined-corr weight |
| `L9_max_new_positions_per_day` | `2` | max 2 new positions/day |
| `L10_max_pct_adv` | `0.05` | max 5% ADV |
| `L11_max_non_aud_exposure` | `0.85` | max non-AUD 85% |

**`limit_set_v2`** (`tools/seed_limit_set_v2.py`, ADR-0014, **derived not restated** — inherits
every v1 key and changes only three): `L5_min_cash_reserve 0.20→0.10`;
`+L2_core_index_etf_weight 0.60` (index-core ETF class cap for SPY/INDA);
`+core_index_etf_allowlist ["SPY","INDA"]`. Governance: supersedes v1, single-confirm allowed
(`confirmation_b` NULL — the `dual_confirm_gap` CHECK only enforces the ≥1h gap when
`confirmation_b` is set, `seed_limit_set_v2.py:19-26`), emits an audit event, and **refuses to
re-run** if a v2 already exists (`seed_limit_set_v2.py:71-78`).

> **Consistency note (v2 vs. ADR-0017):** `limit_set_v2` encodes a 70% ETF core (L2 core-ETF
> cap 0.60, cash floor 0.10). But **ADR-0017 (2026-07-20) retired the ETF core** — the book
> is now satellite-heavy, no ETFs, with the momentum sleeve at 40% and cash ≥10%
> (`bridge.py:216-224`, ground truth §portfolio). So the **index-core keys in the active v2
> limit set are now dormant** (no allowlisted ETF is held), while the L5 cash floor of 0.10
> is the one that binds. This is a real config-vs-policy drift a hostile reviewer will catch:
> the signed limit set still authorises a 60% ETF core that policy no longer uses. It is not
> *unsafe* (an unused authorisation), but it is stale.

### 4.2 Sleeve budget fractions — `SLEEVE_BUDGET_FRACTION` (code constant, ADR-gated)

`atlas/dcp/trading/bridge.py:230-233` — the fraction of NAV each signed strategy **family**
may deploy. Editing it is explicitly "an ADR change" (`bridge.py:229`):

```python
SLEEVE_BUDGET_FRACTION: dict[str, Decimal] = {
    "xsmom-pit-tr": Decimal("0.40"),   # momentum sleeve (ADR-0017)
    "pead-sue-tr":  Decimal("0.00"),   # PEAD sleeve SUSPENDED (ADR-0015)
}
```

- A family **absent** from the dict has no sleeve cap (sized by risk alone). A family at
  **`0.00`** is capital-suspended: membership still records for attribution but never
  allocates (a BUY memo sizes to an honest zero/skip). PEAD sits here because its
  implementable top-5 form failed the null model (p=0.132).
- **Concentration exposure to flag:** with the ETF core retired (ADR-0017), the **entire
  invested book rests on the single `xsmom-pit-tr` sleeve at 40% NAV** (top-5 ⇒ 8%/name =
  exactly the L1 cap). One strategy, one sleeve — a headline concentration risk, by design,
  documented in the ADR.
- Dual-winner names (a symbol in both a momentum and a PEAD top-5) occupy a slot in **both**
  sleeves and size under the **tighter** slice (`bridge.py:270-282`) — conservative
  (under-deploys slightly), never over.

### 4.3 LLM pricing table — `MODEL_RATES_PER_MTOK` (reviewed code constant)

`atlas/agents/runtime/runner.py:56-75`. USD per million tokens (in, out), prefix-matched so
dated snapshot ids price like their alias:

| Model prefix | in / out ($/Mtok) |
|---|---|
| `local/` | 0.0 / 0.0 |
| `claude-opus-4-8` / `-4-7` / `-4-6` | 5.0 / 25.0 |
| `claude-sonnet-5` / `claude-sonnet-4-6` | 3.0 / 15.0 |
| `claude-haiku-4-5` | 1.0 / 5.0 |
| **(unknown / unpriced)** | **15.0 / 75.0, `fail_closed=True`** |

Deliberately **no bare `claude-opus-` catch-all** — an unpriced Opus fails closed at the
legacy $15/$75 rate rather than assuming today's cheaper rate (`runner.py:44-46,52-54`). This
is the honesty backstop for the budget breaker: over-counting is safe, under-counting would
breach the cap. It is a **reviewed constant** ("never scrape", `runner.py:40-41`) — pricing
is code, not fetched at runtime. **Risk:** if Anthropic changes prices, this table is stale
until someone updates it; the fail-closed rate protects the breaker but the *displayed* spend
could diverge from the real invoice.

### 4.4 Other code-constant knobs worth knowing
- `SCHEMA_MAX_ATTEMPTS = 3` (`runner.py:102`) — cage self-correction: 1 call + up to 2
  retries, then hold.
- `TRANSIENT_MAX_ATTEMPTS = 3`, `TRANSIENT_BACKOFF_BASE_S = 1.0`,
  `TRANSIENT_BACKOFF_JITTER_S = 0.25` (`runner.py:94-96`) — HTTP 429/5xx/timeout backoff.
- `DEFAULT_MODEL = "claude-sonnet-4-6"` (`registry.py:31`); `NIGHTLY_WATERMARK_DEFAULT_USD =
  6.00` (`brief.py:64`).
- Scheduler times `CYCLE_UTC = 23:30`, `BACKUP_UTC = 00:30` (`scheduler.py:27-28`) — not
  env-configurable.

---

## 5. Configuration precedence & effective-value cheat sheet

For a Path-A field the effective value is: **`os.environ` › `.env` › code default**.
For a Path-B variable it is: **`os.environ` (populated by `make`/export/launchd/`.env`
sourcing) › code default; a `.env`-only value is invisible unless exported.**

Currently in `.env` (keys only; values redacted — this document does not reproduce secrets):
`ATLAS_DATABASE_URL`, `ATLAS_EODHD_API_KEY`, `ATLAS_ANTHROPIC_API_KEY`, `ATLAS_ALERT_URL`,
`ATLAS_BUDGET_ANALYZE` (=5, per its comment), `ATLAS_INPROC_SCHEDULER`. Everything else runs
on defaults.

**Port note / compose-vs-Mac drift:** `docker-compose.yml` runs the `api` service on
**8000**; the Mac runs it on **8001** (`Makefile:25-26`, because 8000 is taken by another
project on this machine). Anything pointing at `ATLAS_API_URL` must match the *actual* launch
port (default already 8001). The compose `api` service is not the deployment of record on the
Mac.

**Bind-interface drift — the compose services are `0.0.0.0`, not loopback.** `docker-compose.yml`
publishes `db` (`ports: ["5432:5432"]`), `redis` (`["6379:6379"]`) and `api` (`["8000:8000"]`),
and the `Dockerfile`/compose `api` command runs `uvicorn … --host 0.0.0.0`. Docker binds
published ports to the host's `0.0.0.0` by default, so the documented `docker compose up -d db
redis` workflow (CLAUDE.md:29) exposes the **literal-password Postgres (`atlas_local_only`) and
the passwordless Redis on every host/LAN interface — not `127.0.0.1`**. The Mac API run
(`make api` → `uvicorn … --port 8001`, no `--host`) uses uvicorn's loopback default, but the
container image's default is a `0.0.0.0` API bind. Publish to `"127.0.0.1:5432:5432"` /
`"127.0.0.1:6379:6379"` to actually confine these to loopback. See 14_SECURITY §1/§4.1/§5.

---

## 6. Secrets & security posture (state plainly)

- **`.env` is plaintext**, gitignored (`.gitignore:3`), holding the **Anthropic key, EODHD
  key, DB DSN (with password), and alert URL**. No secrets manager, no encryption at rest,
  no vault. Loaded by pydantic-settings (Path A) and sourced by `make`/`backup.sh` (Path B).
- **The DB password is a literal default `atlas_local_only`** — hard-coded in `config.py:8`,
  `docker-compose.yml`, `tools/doctor.py:71`, and `tests/conftest.py:22`. Fine for a
  localhost single-user box; unacceptable the moment Postgres is exposed. It is not a secret
  by any meaningful definition.
- **The API has no authentication or authorization.** No login, session, RBAC, CORS config,
  or TLS. `trading.py:14-15` states the §3.2 `step_up_token`/`scope` plumbing is "deferred to
  the auth phase"; only `acknowledged_risks` is enforced as a body flag
  (`trading.py:52,141,152`). The sole mitigation is that the console is a localhost surface
  for a single Principal.
- **The Anthropic key is Path B** (§3.8): present in `.env` but invisible to
  `os.environ.get(...)` unless the launch exported it — the documented cause of a 401 outage
  after a manual restart. Key rotation is a standing TODO (ground truth §Security).
- **`ATLAS_TRADING_MODE=live` is inert** (§3.3): there is no live path, and `armed` is
  hard-coded `False`. Do not read the health endpoint as evidence of a live-capable system.

---

## 7. Operational reality of the schedule/backup config

The `ATLAS_INPROC_SCHEDULER` + `ATLAS_BACKUP_DIR` + launchd stack is the weakest configured
subsystem, and honesty requires stating it:

- **launchd is dead** on this box — macOS TCC blocks launchd jobs launched from
  `~/Documents` (exit 127 since install), so the "redundant" `com.atlas.daily` /
  `com.atlas.backup` plists installed by `make install-ops` (`Makefile:33-42`) **never ran**.
- Consequently the **only** thing running the nightly cycle and the pg_dump is the
  in-process scheduler (`ATLAS_INPROC_SCHEDULER=1`) inside the API process — a single point
  of failure with no supervisor that works.
- **The fund has taken zero backups to date** (ground truth §Ops); the first ever is
  scheduled via the in-process scheduler. `ATLAS_BACKUP_DIR` therefore points at a directory
  that has, so far, never received a dump.
- **No off-box copy on the Mac.** The `ATLAS_BACKUP_REMOTE` rclone off-box step exists only in
  `ops/backup_linux.sh` (§3.15); the Mac `ops/backup.sh` that actually runs here has no remote
  copy, so even once backups start they are on-disk-only until the Linux migration lands.
- iCloud syncing `~/Documents` produces conflict-copy files under heavy writes; a Linux-box
  migration is **recommended and deferred** by the Principal.
- **Restore drill exists but is manual.** `ops/migrate_from_dump.sh` restores a dump and
  re-verifies the audit chain, gated behind `ATLAS_MIGRATE_FORCE=1` against an existing chain
  (§3.17); it has never been exercised on a real dump because no dump has been produced.

---

## 8. Weaknesses / Debt / Open

- **Two config paths, one silent trap.** Path-A fields honour `.env`; Path-B variables need
  `os.environ` populated (`make`/export/launchd). A bare `uvicorn` launch silently loses the
  Anthropic key, the scheduler, alerts, model overrides, and budget sub-caps. This is not
  theoretical — it caused a 401 outage. **A single loader (e.g. having `Settings` own the
  Anthropic key and having ops read from `Settings`) would remove the trap.**
- **Dead / vestigial config.** `ATLAS_REDIS_URL` is read into `Settings` and injected by
  compose but **no code imports redis** — pure placeholder. `ATLAS_BASE_CURRENCY` and
  `ATLAS_LIMIT_MODE` are **display-only**: FX hard-codes AUD (`fx.py:20`) and the limit-set
  loader ignores `mode` (`engine.py:140`). Setting any of these three misleads.
- **Value drift between code default, `.env`, and reporting default.** Analyze sub-cap is
  $3.00 in code (`runner.py:169`), $5.00 in `.env`, and the brief prints a nightly default of
  $6.00 — the "effective" analyze budget is whatever `.env` currently holds, which no
  reviewer can infer from code alone.
- **Stale signed authorization.** `limit_set_v2` still authorises a 60% ETF index core that
  ADR-0017 retired; the authorization is unused but not withdrawn. Config-vs-policy drift.
- **No hard secret handling.** Plaintext `.env`, literal `atlas_local_only` DB password in
  four places, no secrets manager, no TLS, no API auth. Acceptable only under the
  single-user-localhost assumption; nothing enforces that assumption.
- **Alerting is one optional webhook.** `ATLAS_ALERT_URL` unset (the common state) means the
  only out-of-band signal is stderr; there is no metrics/monitoring stack.
- **Schedule/backup config runs on a broken supervisor.** launchd is TCC-dead; the sole live
  scheduler is in-process; backups have never actually run despite being configured.
- **`get_settings()` re-reads `.env` on every call** (no caching) — negligible cost, but a
  surprising re-parse-per-call for a value most systems would cache once.
- **Compose port (8000) ≠ Mac port (8001).** Mixing the two launch modes mis-points anything
  using `ATLAS_API_URL`.

---

### Appendix — file map for verification

| Concern | File(s) |
|---|---|
| pydantic Settings | `atlas/core/config.py` |
| Model registry / `ATLAS_MODEL_*` / local URL | `atlas/agents/runtime/registry.py` |
| Global budget breaker | `atlas/agents/runtime/budget.py`, `atlas/core/config.py:13` |
| Surface sub-caps + pricing | `atlas/agents/runtime/runner.py:40-75,143-196,270-296` |
| Scheduler switch | `atlas/api/main.py:40-49`, `atlas/ops/scheduler.py` |
| Alerts | `atlas/ops/alerts.py` |
| EODHD adapter selection | `atlas/dcp/market_data/adapters/__init__.py` |
| FX base currency (hard-coded) | `atlas/dcp/market_data/fx.py:20` |
| Limit-set loader (mode-blind) | `atlas/dcp/risk/engine.py:140` |
| Limit set seeds | `seeds/limit_set_v1.json`, `atlas/tools/seed_limit_set_v2.py` |
| Sleeve budgets | `atlas/dcp/trading/bridge.py:216-233,270-282` |
| System/health endpoints (mode, currency) | `atlas/api/routers/system.py` |
| Dashboard client URL | `atlas/dashboard/_client.py:9` |
| Backup dir + off-box copy | `ops/backup.sh` (Mac), `ops/backup_linux.sh` (Linux, `ATLAS_BACKUP_REMOTE`), `Makefile:31` |
| Dump-restore drill + overwrite guard | `ops/migrate_from_dump.sh` (`ATLAS_MIGRATE_FORCE`) |
| Test DB isolation | `tests/conftest.py:20-33` |
| `.env` load + export | `Makefile:2-4` |
| Compose / DB password / redis service | `docker-compose.yml` |
