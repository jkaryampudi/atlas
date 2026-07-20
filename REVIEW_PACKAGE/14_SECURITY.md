# 14 — Security Posture

**Scope of this document.** A code-grounded, deliberately adversarial account of Atlas's
security posture. The objective is to *expose*, not defend. Atlas is a **paper-mode
research/simulation system, months old, one Principal + AI pair, running on a single
laptop**. It moves **no real capital**, holds **no customer data**, and has **no broker
connection** (a `PaperBroker` simulates next-open fills). That threat model is the only
reason the posture below is survivable — and it is stated up front so nothing here reads
as production-grade. **None of what follows should be construed as production-ready.**

Every claim is anchored to a file. Where a claim rests on the ground-truth memo or on
operational fact rather than code, it is tagged **ASSUMPTION** or **OPERATIONAL FACT**.

---

## 0. One-paragraph verdict

Atlas has **no authentication and no authorization on its HTTP API** — the sole control
surface (a FastAPI app on `localhost:8001`) trusts every request that reaches the socket.
Secrets live in a **plaintext `.env`** with a **literal default DB password**
(`atlas_local_only`), no secrets manager, no encryption at rest, and **no TLS**. The
supply chain is **floor-pinned with no lockfile**. The **audit hash-chain is the one
genuine security asset** — but it is **tamper-*evident*, not tamper-*proof***, and its
DB-level append-only enforcement is **scaffolding that the running application does not
actually use**. The entire posture is load-bearing on a single assumption: *one trusted
person, one trusted machine, one loopback interface*. Break that assumption and there is
essentially nothing left — **and the "one loopback interface" leg is already broken by the
repo's own committed artifacts**: `docker-compose.yml` publishes `db` (5432), `redis` (6379)
and `api` (8000) to the host's `0.0.0.0`, and the `Dockerfile`/compose `api` command binds
`--host 0.0.0.0`. Under the documented `docker compose up -d db redis` workflow (CLAUDE.md:29)
the passwordless Redis and the literal-password Postgres are LAN-reachable, not loopback (§1,
§4.1, §5). The 0.0.0.0 exposure below is a **shipped default in the container image**, not a
hypothetical footgun.

---

## 1. Trust model and boundary

The whole design assumes a **single trusted operator on a single trusted host**. There is
no notion of a second, less-trusted user; no tenancy; no network peer that is treated as
hostile. Everything inside the machine is inside the trust boundary.

```mermaid
flowchart TB
  subgraph Trusted["SINGLE TRUSTED HOST — the Principal's MacBook (the ENTIRE trust boundary)"]
    direction TB
    Browser["Browser → console.html<br/>(no login, no session)"]
    API["FastAPI app :8001<br/>NO authn · NO authz · NO CORS cfg · NO rate limit · NO TLS"]
    PG[("Postgres :5432 — compose publishes 0.0.0.0:5432<br/>(LAN-reachable, NOT loopback)<br/>user 'atlas' (schema owner)<br/>pw 'atlas_local_only'<br/>no encryption at rest")]
    Redis[("Redis :6379 — compose publishes 0.0.0.0:6379<br/>(LAN-reachable, NOT loopback)<br/>redis_url default · NO password<br/>DECLARED but UNUSED — no client in app code")]
    ENV[".env (plaintext)<br/>Anthropic key · EODHD key · DB URL"]
    Sched["in-process scheduler<br/>(ATLAS_INPROC_SCHEDULER=1)"]
    Browser -->|HTTP loopback| API
    API --> PG
    API -.reads at startup.-> ENV
    Sched --> API
  end
  API ==>|outbound TLS| Anthropic["api.anthropic.com<br/>x-api-key header"]
  API ==>|outbound TLS| EODHD["eodhd.com/api<br/>?api_token=KEY (in URL)"]

  classDef bad fill:#5b1f1f,stroke:#b33,color:#fff;
  class API,ENV,PG bad;
```

**The "loopback" boundary is already broken by the repo's own compose file — code-true correction.**
The `@ localhost` labels above describe how the *app* dials its dependencies (the DSN says
`@localhost:5432`), **not** what interface those services listen on. The committed
`docker-compose.yml` publishes `db` (`ports: ["5432:5432"]`), `redis` (`["6379:6379"]`) and
`api` (`["8000:8000"]`), and Docker binds published ports to the host's **`0.0.0.0`** by default
— i.e. **every host/LAN interface**, not loopback. CLAUDE.md:29 prescribes the standard local
workflow as **`docker compose up -d db redis`**, so on the actual Mac the Postgres instance
(literal password `atlas_local_only`) and the passwordless Redis are reachable from **any device
on the LAN**, not `127.0.0.1`. The subgraph's "SINGLE TRUSTED HOST = the ENTIRE trust boundary"
premise is therefore **false as shipped**: two of the three data services already extend past the
host to the local network. (The Mac *API* process, launched via `make api` →
`uvicorn … --port 8001` with no `--host`, does still bind uvicorn's loopback default — see §5 —
but its own container image does not; see below.) Hardening: publish only to loopback, e.g.
`"127.0.0.1:5432:5432"` / `"127.0.0.1:6379:6379"`.

**Implication.** The trust boundary is the *host*, not the *user* — **and, per the correction
above, not even fully the host**: the DB and Redis ports are LAN-published. Any process, any other
person at the keyboard, anything that can open a TCP connection to `127.0.0.1:8001` (SSH
tunnel, a malicious local dependency, a browser tab exploiting the lack of CORS/origin
checks on a non-preflighted request) inherits **full operator authority**: approve trades,
clear risk breakers, spend the LLM budget, run the daily pipeline.

---

## 2. Authentication — **NONE** (stated bluntly)

**There is no request authentication anywhere in the API.** [IMPLEMENTED — as *absence*]

- `atlas/api/main.py` constructs `FastAPI(...)` and includes eleven routers
  (`atlas/api/main.py:52`, `:70`–`:80`). There is **no `add_middleware`, no
  authentication dependency, no `Depends(...)` security scheme, no API-key check, no
  session, no login route** anywhere in `atlas/api/**`. A repo-wide grep for
  `add_middleware|CORSMiddleware|TrustedHost|HTTPSRedirect|oauth|authenticat|bearer|login|session`
  returns **zero** hits in application code.
- The console (`atlas/dashboard/console.html`) is served unauthenticated from
  `GET /console` (`atlas/api/main.py:60`) and is a **pure API client** — it holds no
  token and presents no credential (grep for `api_key|token|secret|authorization` in the
  console returns only table headers, `atlas/dashboard/console.html:1323`).
- The interactive OpenAPI docs (`/docs`, `/openapi.json`) are exposed by default and are
  likewise unauthenticated — a full self-describing map of every mutating endpoint.

There is no MFA, no step-up, no device binding. The *only* thing standing between an
arbitrary caller and the trade-approval desk is **the network reachability of port 8001**.

---

## 3. Authorization — **NONE** (single-principal; step-up token *deferred, not built*)

There is no role model, no scope check, no per-action privilege. The code says so
explicitly. From the trading router docstring:

> "Paper mode v1: single principal on a local console — §3.2's step_up_token / scope
> plumbing is **deferred to the auth phase**; acknowledged_risks IS enforced."
> — `atlas/api/routers/trading.py:14`

The **only** authorization-shaped control that *is* wired is a boolean self-attestation
flag on the approve body:

```python
class ApproveBody(BaseModel):
    acknowledged_risks: bool = False
```
— `atlas/api/routers/trading.py:140`. If `false`, the lifecycle raises and the router maps
it to `400 RISKS_NOT_ACKNOWLEDGED` (`trading.py:52`, `:151`–`:154`). This is a
**speed-bump, not authorization**: the caller sets their own flag. There is no
`step_up_token`, no scope, no signer identity, no proof that the human — as opposed to
any client — pressed the button.

### 3.1 Every state-changing endpoint runs with zero authorization

All **16 `POST` endpoints** below execute for **any** caller who reaches the port (a repo-
wide grep for `@router.post` across `atlas/api/routers/**` returns exactly 16). None
verifies *who* is calling; none requires a credential. **Fifteen** of the 16 mutate state;
the sixteenth (`/risk/preflight`, `risk.py:195`) is a strictly **read-only** what-if dry
run whose docstring guarantees **ZERO writes** — it `session.rollback()`s before returning
to make that structural — yet it still runs unauthenticated, which is why it is listed
here.

| Endpoint | File:line | What an unauthenticated caller can do |
|---|---|---|
| `POST /v1/system/run-daily` | `system.py:70` | Fire the entire T0–T9 daily pipeline on demand |
| `POST /v1/research/analyze` | `research.py:103` | Trigger a live committee run → **LLM spend** (burns the daily budget) |
| `POST /v1/research/opportunities/run` | `research.py:147` | Whole-universe screen compute |
| `POST /v1/research/opportunities/track` | `research.py:177` | Persist tracking rows |
| `POST /v1/research/source-picks/ingest` | `research.py:215` | Ingest external picks |
| `POST /v1/research/source-picks/grade` | `research.py:262` | Write-once grade rows |
| `POST /v1/research/memos/{id}/review` | `research.py:588` | Mutate memo review state |
| `POST /v1/trading/proposals/{id}/approve` | `trading.py:144` | **Approve a trade** (paper book) |
| `POST /v1/trading/proposals/{id}/reject` | `trading.py:171` | Reject a proposal |
| `POST /v1/trading/orders/{id}/cancel` | `trading.py:189` | Cancel an order |
| `POST /v1/trading/positions/{id}/close` | `trading.py:227` | Open a discretionary EXIT proposal |
| `POST /v1/trading/settle` | `trading.py:264` | Expire proposals + fill pending orders |
| `POST /v1/risk/breaker-clearances` | `risk.py:133` | Propose clearing a **latched drawdown breaker** |
| `POST /v1/risk/breaker-clearances/{id}/confirm` | `risk.py:146` | Confirm the clearance (dual-confirm, but no identity behind either half) |
| `POST /v1/risk/preflight` | `risk.py:195` | Run a risk preflight — strictly **read-only** dry run (ZERO writes; `session.rollback()` before return). Still unauthenticated. |
| `POST /v1/factory/recipes/run` | `factory.py:79` | Launch a research gauntlet → **compute/LLM burn** |

**Cost / DoS note.** `POST /research/analyze` and `POST /factory/recipes/run` spend real
money against the Anthropic key. The **only** brake is the LLM cost circuit-breaker
(`daily_llm_budget_usd = 10.0`, `atlas/core/config.py:13`, with nightly/analyze/shadow
sub-caps). There is **no request rate-limit and no per-caller quota** — an unauthenticated
loop can exhaust the daily budget in seconds, a low-effort denial-of-service against the
research desk. Grep for `rate.?limit|slowapi|limiter|throttle` in `atlas/api/**` returns
**nothing**.

**Risk-breaker note.** The DD-breaker clearance is *dual-confirm* by design
(`risk.py:133`/`:146`, ADR-referenced `risk/clearance.py`), but "dual-confirm" here means
*two API calls*, not *two authenticated identities*. A single unauthenticated caller makes
both calls. Invariant 3 ("Risk FAIL is terminal") holds against the *risk engine* — the
approve path re-runs the check on a fresh snapshot and a FAIL commits the void
(`trading.py:146`–`:159`) — but nothing authenticates the human who clears a latched
breaker.

---

## 4. Secrets management — plaintext, literal defaults, no manager, no KMS

### 4.1 What secrets exist and where they live

- **DB credentials** — hardcoded default in code:
  ```python
  database_url: str = "postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas"
  ```
  — `atlas/core/config.py:8`. The **password `atlas_local_only` is a literal in a public
  repo** (see §6). It is a "local only" throwaway, but it is real, committed, and used by
  the app unless overridden.
- **EODHD market-data key** — `eodhd_api_key: str = ""` (`config.py:14`), read from the
  environment via `pydantic-settings` (`env_prefix="ATLAS_"`, `env_file=".env"`,
  `config.py:6`).
- **Anthropic API key** — read directly from the process environment as
  `ATLAS_ANTHROPIC_API_KEY` (`atlas/agents/runtime/registry.py:62`,
  `atlas/agents/shadow_compare.py:299`, `atlas/ops/daily.py:695`). It is **not** modelled
  in `Settings`; the agent runtime reads it out of `os.environ` at call time.
- **Redis URL** — `redis_url: str = "redis://localhost:6379/0"` (`config.py:9`), a
  **no-password** connection string. The DSN dials `localhost`, but the backing `redis:7`
  service is **not on loopback**: `docker-compose.yml` publishes it with `ports: ["6379:6379"]`,
  which Docker binds to the host's **`0.0.0.0`** — i.e. **every LAN interface**. So the
  passwordless Redis started by the documented `docker compose up -d db redis` workflow
  (CLAUDE.md:29) is reachable from any device on the local network, not just `127.0.0.1`.
  **No application code imports or connects to Redis** — a grep for `import redis`/`from redis`
  across `atlas/` returns nothing; the only reference is a `docker compose up -d db redis` hint
  string in `doctor.py:44`. Redis is **declared-but-unused**: a config default and a running
  container with no live client. Attack value is low *today* (nothing reads or writes it), but
  it is an **unauthenticated, LAN-published** service and a latent surface the moment any code
  wires it in. Hardening: publish to `"127.0.0.1:6379:6379"`.

### 4.2 How they are loaded

`pydantic-settings` reads a **plaintext `.env`** file (`config.py:6`). The file exists on
the host (`.env`, ~424 bytes) and is **git-ignored** (`.gitignore` line `.env`;
`git check-ignore .env` confirms). There is:

- **no secrets manager** (no Vault / AWS Secrets Manager / SOPS / age),
- **no KMS or envelope encryption**,
- **no encryption of the `.env` at rest** (it is a plaintext file on an unencrypted-app
  path; FileVault, if enabled, is the only at-rest protection and is an **ASSUMPTION**
  about the host, not a code control),
- **no key rotation mechanism** (rotation is a standing manual TODO — **OPERATIONAL
  FACT** per the handoff memo),
- **no separation** between the process that needs the Anthropic key and the process that
  needs the EODHD key and the process that needs the DB password — one `.env`, one blast
  radius.

### 4.3 Secret-handling footguns observed in code

- **Anthropic key not auto-loaded into `os.environ`.** `pydantic-settings` populates
  `Settings`, but the agent runtime reads `os.environ["ATLAS_ANTHROPIC_API_KEY"]`
  directly (`registry.py:62`). A manual process restart that does not re-export the var
  drops the key → the agent runtime authenticates with `""` → Anthropic returns 401.
  This exact failure occurred in operation (**OPERATIONAL FACT**, handoff memo). It is a
  *reliability* bug, but it is also a *secret-provisioning fragility*: there is no single
  authoritative loader.
- **EODHD key travels in the URL query string.** The adapter sends the key as
  `params={"api_token": self._key, ...}` on `GET {BASE}{path}`
  (`atlas/dcp/market_data/adapters/eodhd.py:91`–`92`, `BASE = "https://eodhd.com/api"`,
  `:19`). Query-string secrets are the classic "leaks into access logs / proxy logs /
  browser history" pattern. Here the request is server-side outbound over TLS, so the
  exposure is limited to any local egress proxy or the vendor's own logs — but it is
  inherently weaker than a header credential. (This is EODHD's API design, not Atlas's
  choice; noted for completeness.)
- **Anthropic key is sent correctly** as an `x-api-key` header over TLS
  (`atlas/agents/runtime/llm.py:64`–`66`) — the right pattern.

---

## 5. Transport security — **no TLS on the API**

The API is served by plain `uvicorn atlas.api.main:app --port 8001`
(`Makefile:26`) — **no `--ssl-keyfile`/`--ssl-certfile`, no reverse proxy terminating
TLS**. All console↔API traffic is **cleartext HTTP over loopback**. That is acceptable
*only* because it never leaves `localhost`; the moment the port is bound to a routable
interface or tunnelled, every request (including the `acknowledged_risks` approvals and
any future step-up token) is on the wire in the clear.

- The **Mac** API run (`make api` → `uvicorn atlas.api.main:app --port 8001`, no `--host`,
  `Makefile:26`) does rely on uvicorn's loopback default; nothing in code pins `host=127.0.0.1`.
  **ASSUMPTION:** uvicorn's default host keeps *that* process on loopback; there is no
  code-level guard forcing it.
- **But 0.0.0.0 exposure is NOT hypothetical — it is a shipped default in the committed
  container artifacts.** The `Dockerfile` CMD is
  `["uvicorn","atlas.api.main:app","--host","0.0.0.0","--port","8000"]` (with `EXPOSE 8000`),
  and the `docker-compose.yml` `api` service runs
  `uvicorn atlas.api.main:app --host 0.0.0.0 --port 8000` and publishes `ports: ["8000:8000"]`.
  Anyone who runs the container image or the `api` compose service gets the **unauthenticated
  API bound to `0.0.0.0` and LAN-published** — no "stray flag" required; it is the default in
  the repo. Separately (see §1, §4.1), the `db` and `redis` services the documented workflow
  *does* start (`docker compose up -d db redis`, CLAUDE.md:29) are already published to the
  host's `0.0.0.0`. The correct framing is therefore: **loopback is not the security control
  the doc's §0 verdict claims — the repo's own compose/Docker defaults already reach the LAN.**
  Hardening: set `--host 127.0.0.1` in the Dockerfile/compose command and publish ports only to
  `127.0.0.1` (e.g. `"127.0.0.1:8000:8000"`).
- **Outbound** transport is fine: Anthropic and EODHD calls use `httpx` with default
  certificate verification on (`llm.py:61`, `eodhd.py`), i.e. TLS is enforced going out.

---

## 6. Supply chain

### 6.1 Public repository

The repo is **public on GitHub** (`https://github.com/jkaryampudi/atlas`). Consequences:

- The **literal DB password** `atlas_local_only` (`config.py:8`) and the CI DB password
  `atlas` (`.github/workflows/ci.yml`) are world-readable. Both are throwaways, but they
  advertise the credential-hygiene pattern (weak literals) to anyone reading.
- No `.env` is tracked (`git check-ignore .env` confirms; a history scan of the sampled
  revisions surfaced no `sk-ant…`/EODHD-shaped literals) — **so no live key is known to be
  committed**, but this was verified over a *sample* of history, not exhaustively. A full
  `trufflehog`/`gitleaks` sweep of all 137 commits is **NOT** something this review ran and
  **should** be run before the repo is trusted as clean. [PARTIAL verification]

### 6.2 Dependency pinning — **floors only, no lockfile**

`pyproject.toml` declares dependencies with **lower-bound (`>=`) floors and no upper
bounds, and there is no lockfile** anywhere in the tree (no `uv.lock`, `poetry.lock`,
`requirements.txt`, or hashes):

```
fastapi>=0.115 · pydantic>=2.8 · pydantic-settings>=2.4 · sqlalchemy>=2.0 ·
alembic>=1.13 · psycopg[binary]>=3.2 · redis>=5.0 · uvicorn>=0.30 ·
httpx>=0.27 · exchange_calendars>=4.5      (dev: pytest/pytest-cov/ruff/mypy/hypothesis, also >=)
```
— `pyproject.toml:6`–`21`.

**Risk.** `pip install -e ".[dev]"` (used verbatim by CI, `ci.yml`) resolves to *whatever
is latest* satisfying each floor. This means:

- **Non-reproducible builds** — two installs on different days can differ.
- **No hash pinning** → exposure to a compromised or malicious upstream release (a
  poisoned point-release of any transitive dependency installs silently).
- **No SBOM, no dependency scanning** (no Dependabot config, no `pip-audit` step in CI).

### 6.3 Hand-rolled Anthropic client

The Anthropic integration is **not** the official SDK — the `anthropic` package is **not
even a declared dependency**. `AnthropicClient` is a hand-written `httpx.post` to
`https://api.anthropic.com/v1/messages` with a pinned `anthropic-version: 2023-06-01`
header (`atlas/agents/runtime/llm.py:53`–`75`). Trade-offs, stated honestly:

- **Pro (supply chain):** one fewer third-party package with transitive deps and update
  churn; the transport seam is injectable for tests.
- **Con:** the team owns error handling, retry semantics, `retry-after` honouring, and
  API-version drift by hand. There is no SDK-level guardrail; a breaking API change is the
  team's problem. The version header is a static string that will silently rot.

### 6.4 GitHub Actions supply chain

`ci.yml` uses `actions/checkout@v4` and `actions/setup-python@v5` — **pinned to moveable
major-version tags, not commit SHAs**. A compromised or force-moved tag would execute
attacker-controlled steps in CI (which holds no production secrets here, but does have
`GITHUB_TOKEN`). SHA-pinning is the hardening this lacks.

---

## 7. Injection & OWASP-style findings

### 7.1 SQL injection — **parameterized throughout; no user data interpolated into SQL**

All queries use SQLAlchemy `text()` with **bound parameters** (`:name` + a params dict).
Representative: the proposal list builds its `WHERE`/`LIMIT` from bound params
(`atlas/api/routers/trading.py:80`–`87`, `:200`–`:220`), never string-formatting the
user's `state`/`limit` into the SQL. A repo-wide grep for the dangerous pattern —
`text(f"…")` / `execute(f"…")` — returns **zero** hits across all of `atlas/`.

**Three files interpolate identifiers into SQL — and all three interpolate FIXED
CONSTANTS, not user input** (audited individually, judged safe):

- `atlas/dcp/research/source_picks.py:275`–`308` builds column lists (`excess_{h}`) and
  `SET`/`WHERE` fragments from `PICK_HORIZONS` — a **module constant**, not request data.
  Values are still bound (`:{c}`, `:id`, `:ts`).
- `atlas/dcp/market_data/estimate_snapshots.py:354`,`:369` interpolates `_ROW_COLUMNS` — a
  **module constant**.
- `atlas/agents/evals/run.py:202` interpolates a `where` fragment assembled from internal,
  non-request-derived values.

**Verdict:** no first- or second-order SQL injection reachable from the HTTP surface was
found. This is a genuine strength of the data-access style. (It rests on discipline, not a
lint rule — a future contributor could regress it; a `bandit`/`ruff-S` gate on
`text(f"…")` would make it structural.) [IMPLEMENTED — parameterization]

### 7.2 CORS — **not configured** (default-deny for credentialed cross-origin)

No `CORSMiddleware` is installed (grep: zero hits). FastAPI's default is **no CORS
headers**, so browsers block cross-origin *reads*. Because the API also uses **no cookies
and no `Authorization` header**, there is no ambient credential for a malicious site to
ride — so **CSRF is largely moot** *today*. **But**: simple (non-preflighted) requests —
e.g. a `POST` with `Content-Type: text/plain` — are *sent* by the browser even if the
response is unreadable. Against endpoints that mutate state on the request alone
(`/settle`, `/run-daily`), a drive-by page on the same machine could **fire side-effects
blind**. The absence of CORS config is protective for reads but is **not** a substitute
for auth on writes.

### 7.3 Input validation

- Pydantic bodies validate types (`ApproveBody`, `RejectBody`, `CloseBody`,
  `trading.py:140`,`:167`,`:223`; factory `spec: dict` with a grammar, `factory.py`).
- Path/query params (`proposal_id: str`, `limit: int`, `state: str | None`) are
  type-coerced by FastAPI but **not otherwise constrained** — e.g. `limit` has no upper
  bound (`trading.py:80`, `:201`), so a caller can request `LIMIT 2000000000` and force a
  large scan. Low severity (single trusted host), but it is an unbounded-resource input.
- `proposal_id` is passed as a `str` bound parameter into a `uuid`-typed column; a
  malformed value yields a DB cast error surfaced as a 500, not a crash — acceptable but
  noisy.

### 7.4 SSRF / egress

Outbound HTTP targets are **fixed constants** — `api.anthropic.com` (`llm.py:65`) and
`eodhd.com/api` (`eodhd.py:19`). No endpoint fetches a **caller-supplied URL**, so there
is **no SSRF surface** from the HTTP API. The `OpenAICompatClient` base URL
(`llm.py:82`) is operator-configured (LAN model box), not request-derived.

### 7.5 Error handling / information disclosure

Errors are mapped to a uniform envelope `{error:{code,message,details}}`
(`trading.py:42`–`54`). Un-mapped exceptions (e.g. a lineage-verification `RuntimeError`
in `/settle`, `trading.py:264`–`269`) propagate as **500s with FastAPI's default body** —
in a default uvicorn run the stack trace goes to server logs, not the client, so
disclosure is limited. `/docs` and `/openapi.json` being open is the larger disclosure:
the full API shape is public to anyone on the port.

### 7.6 OWASP API Security Top 10 (2023) — quick map

| # | Category | Atlas status |
|---|---|---|
| API1 | Broken Object-Level Authz | **N/A-by-absence** — there is no authz at all; any object is reachable by id |
| API2 | Broken Authentication | **FAIL** — no authentication exists |
| API3 | Broken Object Property-Level Authz | **FAIL** — no field-level controls; full rows returned |
| API4 | Unrestricted Resource Consumption | **FAIL** — no rate limit, no `limit` cap; LLM-spend endpoints open |
| API5 | Broken Function-Level Authz | **FAIL** — every mutating function is callable by anyone |
| API6 | Unrestricted Access to Sensitive Business Flows | **FAIL** — trade approval / breaker clearance fully open |
| API7 | SSRF | **PASS** — fixed outbound targets, no caller-supplied URLs |
| API8 | Security Misconfiguration | **FAIL** — no TLS, no CORS policy, `/docs` open, literal secrets |
| API9 | Improper Inventory Management | **PARTIAL** — single documented surface, but `/docs` fully exposed |
| API10 | Unsafe Consumption of APIs | **PARTIAL** — hand-rolled Anthropic client; EODHD key in URL |

The saving grace across every FAIL row is identical: **the network boundary (loopback +
single trusted host)** is doing all the work that authn/authz would otherwise do.

---

## 8. Audit hash-chain — the one genuine strength, scoped honestly

Credit where due: `audit.decision_events` is a **real, tested, tamper-evident hash
chain**, and it is the strongest security control in the system.

**How it works.**
- Each event's `payload_hash = sha256(canonical_json(payload))` with deterministic
  serialization (sorted keys, no whitespace variance) — `atlas/core/audit.py:24`–`31`.
- Each event links to the prior via
  `link_hash(prev_hash, payload_hash, event_type, created_at)`
  (`audit.py:34`–`36`, `:52`–`54`). Mutating any historical payload changes its
  `payload_hash`, which breaks **every subsequent** `prev_hash` link.
- Appends are serialized under a **transaction-scoped Postgres advisory lock**
  (`pg_advisory_xact_lock`, `atlas/core/audit_repo.py:29`) so concurrent writers cannot
  fork the chain.
- `verify_chain` re-walks the chain, re-hashing every payload and every link, and
  **detects both tampering and deletion** (a removed row breaks the next row's link;
  non-contiguous seqs from rolled-back txns are tolerated) — `audit.py:95`–`115`.
- A nightly job (`atlas/tools/verify_chain.py`) re-walks the DB chain, **appends its own
  verification event** (so silent gaps in verification are themselves visible), and
  **exits non-zero on any break** with the breach on stderr (`verify_chain.py:35`–`46`).

```mermaid
sequenceDiagram
  participant W as Writer (dcp/agent/human/scheduler/broker)
  participant L as PostgresAuditLog
  participant DB as audit.decision_events
  participant V as verify_chain (nightly)
  W->>L: append(event_type, payload, ...)
  L->>DB: pg_advisory_xact_lock(762001)
  L->>DB: SELECT last row (seq, hashes, ts)
  L->>L: prev = link_hash(last)  ·  p_hash = sha256(payload)
  L->>DB: INSERT (payload, payload_hash, prev_hash, created_at)
  Note over V,DB: later, nightly
  V->>DB: SELECT * ORDER BY seq
  V->>V: re-hash payloads, re-link; raise on any mismatch
  V->>DB: APPEND audit.chain.verified (actor=scheduler)
  V-->>V: exit != 0 on break → alert
```

### 8.1 Honest limits — tamper-*evident*, not tamper-*proof*

The chain proves *that* something changed; it does **not prevent** the change, and it is
**not** cryptographically notarized.

1. **DB append-only is scaffolding the app does NOT use.** Migration 0001 creates an
   `atlas_audit_writer` role with **`GRANT INSERT` only** (no UPDATE/DELETE) and comments
   it "INSERT-only (Doc 05 par.6)" — `migrations/versions/0001_initial.py:108`–`123`. **But
   the role is `NOLOGIN`, and the application connects as user `atlas` — the schema owner**
   (`config.py:8`). A grep for `SET ROLE`/`REVOKE` across `atlas/` and `migrations/`
   returns **nothing**. Therefore, **at runtime the connecting user can `UPDATE`/`DELETE`
   `audit.decision_events` freely** — nothing at the database layer prevents mutation. The
   append-only property is enforced by **(a) application discipline** (only
   `PostgresAuditLog.append` writes; there is no UPDATE/DELETE code path) and **(b) the
   hash chain making any out-of-band mutation *detectable at the next verify*** — **not**
   by privilege. This gap should be stated in any claim that the audit log is
   "append-only": it is append-only *by convention + detection*, not by enforcement.
   For completeness, `atlas_audit_writer` is **not** the only least-privilege role in the
   scaffold: migration 0001 also creates a sibling read role **`atlas_agent_reader`**
   (`SELECT`-only, `0001_initial.py:116,121-122`), actively maintained with SELECT grants
   across **~14 migrations** (0001–0004, 0010, 0013–0016, 0019, 0020, 0024, 0025, 0027,
   0029, 0031, 0033) and deliberately given **no** grants on `risk.*` (Constitution §3.2,
   `0001_initial.py:123`). It is equally `NOLOGIN` and equally never `SET ROLE`d, so it too
   is present-but-inert — the two roles are a **fully-granted least-privilege scaffold that
   nothing at runtime assumes**, not merely one inert audit role. The third role the design
   promised (`dcp_writer`) was never built (grep: none).
2. **Not signed, not notarized, not externally anchored.** The chain is plain SHA-256 with
   **no HMAC key, no digital signature, and no external timestamp/anchor** (no RFC-3161
   TSA, no public-ledger anchoring). An attacker with write access who is willing to
   **recompute the whole tail** (re-hash every event after the tampered one, and every
   `chain_hash`) can forge a **fully self-consistent** chain that `verify_chain` accepts.
   The only defenses against a full rewrite are (i) the attacker not knowing the algorithm
   (they do — it is in a public repo) and (ii) an **external copy** to diff against. There
   is currently **no off-host anchor** of the chain head, so a full rewrite on the single
   host is undetectable. (Backups would help — but see §9: backups have never run.)
3. **Single-node, single-writer.** All integrity rests on one Postgres instance on one
   laptop. There is no replication, no WORM storage, no second observer.
4. **`created_at` is part of the link but injectable.** Timestamps come from the injectable
   clock (`audit_repo.py:36`), correct for determinism/testing, but it means the time bound
   into the link is only as trustworthy as the clock the writer supplies.

**Bottom line:** excellent *tamper-evidence for accidental corruption and casual tampering,
verified nightly*; **not** a defense against a determined insider with DB write access, and
**not** the "tamper-proof / immutable ledger" that phrase sometimes implies.

---

## 9. Infrastructure & operational security

- **Single machine, single interface.** The Principal's MacBook; Postgres in Docker; API
  on `localhost:8001`. The **API process *is* the scheduler** when
  `ATLAS_INPROC_SCHEDULER=1` (`atlas/api/main.py:40`–`49`) — the same unauthenticated
  process that serves the console also runs the nightly cycle and (nominally) backups.
- **launchd supervision is dead → zero backups taken.** macOS TCC blocks launchd from
  `~/Documents` (exit 127 since install), so the "redundant" scheduler and the nightly
  `pg_dump` **never ran**. **The fund has taken ZERO database backups to date**
  (**OPERATIONAL FACT**, handoff memo). Security consequence: there is **no off-host copy
  of the audit chain** to diff against — directly weakening §8's tamper-evidence (a full
  local rewrite has nothing to be caught against). When backups do begin, note that
  `pg_dump` output is **plaintext, unencrypted** — a backup is a full copy of every secret
  stored in the DB and every audit payload, with no at-rest protection.
- **iCloud sync of `~/Documents`** produces conflict-copy files under heavy writes
  (**OPERATIONAL FACT**) — a data-integrity hazard and an inadvertent exfiltration path
  (DB dumps / `.env`-adjacent files syncing to a cloud account).
- **No encryption at rest** on Postgres or on the app directory beyond whatever
  FileVault the host provides (**ASSUMPTION** about the host, not a code control).
- **CI *runs* but does not *gate* — no merge control on `main`.**
  `.github/workflows/ci.yml` runs `ruff` + `mypy` + `pytest` + `alembic upgrade head` on every
  `push` and `pull_request`, with a Postgres 16 service. There is **no CD**: no automated deploy,
  no image build, no signed release, no environment promotion. (See §12 — this *contradicts* the
  ground-truth memo, which states "No CI/CD pipeline that runs the suite on push.") Security-
  relevant caveat: the workflow **runs and reports, it does not enforce**. There is **no
  branch-protection rule, no required-status-check, and no CODEOWNERS anywhere in-tree** (`find
  .github -type f` → only `workflows/ci.yml`; those controls live in GitHub repo *settings*, not
  the tree, and none is asserted), and the history is a **linear, single-author direct-to-main
  flow with zero merge/PR commits** (`git log --merges` empty). A GitHub Actions run cannot block
  a `git push` to `main`, so CI is a **post-push signal, not a gate** — a red (or, in a
  compromised-contributor scenario, a malicious) commit lands on `main` uncontested, and CI would
  only surface it after the fact.
- **Alerting is optional and often off.** The only alert channel is an ntfy webhook
  (`ATLAS_ALERT_URL`), which degrades to stderr when unset (**OPERATIONAL FACT**). A
  security-relevant failure (audit-chain break, reconciliation break) may surface only on a
  console nobody is watching.

---

## 10. Live-trading arming gate — designed, **NOT BUILT**

`trading_mode` defaults to `"paper"` and the config comment notes "live additionally
requires daily arming" (`atlas/core/config.py:10`). **Live mode is unbuilt** — there is no
broker integration, and the human-arming control that would gate real capital is a
**[PLANNED — NOT BUILT]** design, not code. This matters for security review only insofar
as it must not be *mistaken* for a shipped safety control: today the "arming" gate does not
exist to be bypassed because live trading itself does not exist. The `PaperBroker`
simulates fills; nothing touches a real account.

---

## 11. What an attacker actually gets (by position)

```mermaid
flowchart LR
  A["Reaches 127.0.0.1:8001<br/>(local shell, SSH tunnel,<br/>malicious local dep, drive-by tab)"] --> B{No authn / authz}
  B --> C["Approve/reject trades<br/>(paper book)"]
  B --> D["Clear latched risk breakers"]
  B --> E["Burn LLM budget → DoS<br/>(/analyze, /factory run)"]
  B --> F["Run daily pipeline / settle"]
  B --> G["Read full audit chain,<br/>memos, positions via /docs"]
  H["DB write access<br/>(owner conn, no least-priv)"] --> I["Rewrite audit tail<br/>(no off-host anchor to catch it)"]
```

- **Reach the port** → full operator authority (§2–§3). The realistic vectors on a laptop
  are: another local process, an SSH port-forward, a malicious PyPI dependency executing in
  the venv, or a browser tab issuing non-preflighted `POST`s.
- **Reach the DB** (same creds, same host, literal password) → read every secret-bearing
  and audit row, and — because the app connects as owner — **mutate the audit chain**;
  detection depends entirely on the nightly `verify_chain` *and* on an external copy that
  currently does not exist.

---

## 12. Cross-document inconsistency noticed

**Ground-truth vs. code on CI.** `00_GROUND_TRUTH.md` states: *"No CI/CD pipeline that runs
the suite on push … 'CI green' claimed in an ADR but verify."* The code **contradicts** the
first half: `.github/workflows/ci.yml` **does** run the full suite (`ruff`, `mypy`,
`pytest`) plus a migration apply-check on **every `push` and `pull_request`**, against a
Postgres service container. So **CI that runs the suite on push exists and is real** — the
memo under-claims it. What genuinely does **not** exist is **CD** (deployment automation,
image builds, signed releases). Recommend correcting the memo to "CI runs the suite on
push; **no CD**." (Whether CI is *currently green* is a separate runtime fact this static
review did not execute.)

---

## 13. Weaknesses / Debt / Open

**Critical (would be release-blocking outside paper mode):**
- **No authentication on the API** (§2). Every endpoint, including trade approval and risk-
  breaker clearance, is open to anyone on the port.
- **No authorization / no step-up** (§3) — `step_up_token`/scope explicitly *deferred*
  (`trading.py:14`); `acknowledged_risks` is self-attested, not authenticated.
- **No TLS** (§5) — cleartext HTTP; nothing in code pins the bind to loopback. Worse, the
  committed `Dockerfile`/`docker-compose.yml` **already** bind the API to `--host 0.0.0.0` and
  publish `db`/`redis`/`api` (5432/6379/8000) to the host's `0.0.0.0`, so the documented
  `docker compose up -d db redis` workflow makes the passwordless Redis and literal-password
  Postgres LAN-reachable today (§1, §4.1, §5) — the loopback assumption is not the control the
  §0 verdict treats it as.
- **Plaintext secrets, literal DB password in a public repo, no secrets manager, no at-rest
  encryption, no rotation** (§4, §6.1).
- **Audit append-only is not enforced at runtime** (§8.1) — role scaffolding exists but the
  app connects as owner and never `SET ROLE`; the chain is tamper-*evident*, not
  tamper-*proof*, and **not** signed/notarized/externally anchored.
- **Zero off-host copy of the audit chain** because **backups have never run** (§9) — this
  removes the external anchor that would catch a full local chain rewrite.

**High:**
- **No rate limiting / unbounded `limit`** (§3, §7.3) → cheap LLM-budget and DB DoS.
- **Floor-only dependency pins, no lockfile, no hash pinning, no `pip-audit`/Dependabot**
  (§6.2) → non-reproducible builds and upstream-compromise exposure.
- **`/docs` + `/openapi.json` open** (§7.5) → full unauthenticated API map.
- **GitHub Actions pinned to moveable tags, not SHAs** (§6.4).

**Medium / hygiene:**
- **EODHD key in URL query string** (§4.3) — weaker than a header credential.
- **Anthropic key not loaded through a single authoritative path** (§4.3) → the 401-on-
  restart failure class.
- **Hand-rolled Anthropic client** with a static API-version header (§6.3) — owns retry/
  version drift by hand.
- **CORS unconfigured** (§7.2) — protective for reads today, but non-preflighted `POST`
  side-effects remain possible from a local drive-by page.
- **iCloud sync of the working tree** (§9) — integrity + inadvertent-exfiltration hazard.
- **`pg_dump` backups will be plaintext** once they run (§9).
- **No exhaustive secret-scan of git history** performed by this review (§6.1) — run
  `gitleaks`/`trufflehog` over all 137 commits before trusting the repo as clean.

**Cheap hardening that would move the needle most (not prescriptions, observations):**
a single shared-secret bearer check or localhost-only `TrustedHostMiddleware`; binding the
DB app-connection to a least-privileged role so the owner isn't the runtime user;
`SET ROLE atlas_audit_writer` (or a `BEFORE UPDATE/DELETE` trigger) on the audit table to
make append-only *enforced*; an off-host copy of the chain head; a lockfile with hashes;
and disabling `/docs` in any non-loopback deployment.

---

*Prepared for adversarial institutional review. Paper-mode, single-Principal, single-host
research system. No real capital, no customer data, no broker connection. Nothing herein is
investment advice.*
