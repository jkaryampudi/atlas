# 15 — Performance, Scaling & Resource Profile

**Status of this document:** Adversarial self-assessment for the review committee.
**Scope caveat (read first):** Atlas is a **paper-mode research/simulation system, months
old, running on one Principal's MacBook with Postgres in Docker**. There is **no formal
performance test tier** — no benchmark harness, no load tests, no latency SLOs, no
profiling in CI. The numbers below are a mix of **(A) live measurements I took during this
review** (labelled **[MEASURED — this session]**, machine warm, single run each — *not*
statistically rigorous) and **(B) prior observations / order-of-magnitude estimates from
the ground-truth notes** (labelled **[OBSERVED/ESTIMATED]**). Treat every wall-clock figure
as indicative, not a committed benchmark. Where a claim is purely structural (from reading
the code), it is labelled **[STRUCTURAL]**.

Measurement host & conditions for all **[MEASURED]** rows: Apple Silicon MacBook, Python
3.14, Postgres 16 in Docker (container up 9 days, page cache warm), SQLAlchemy 2.0 +
psycopg 3, single process, no concurrent load other than my own probes. Dev DB `atlas`:
708 instruments, **2,466,700** `price_bars_daily` rows, 526 fundamentals snapshots, 1,884
audit events, 511 active US instruments (verified this session).

---

## 0. Executive summary

| Dimension | Reality | Evidence |
|---|---|---|
| Formal perf testing | **None.** No benchmark/load/stress tier; `pytest` addopts is just `-q`. | `pyproject.toml` `[tool.pytest.ini_options]`; no `pytest-benchmark`/`pytest-xdist` installed |
| Parallelism | **Effectively none for compute.** No `numpy`/`pandas`, no `multiprocessing`, no `concurrent.futures`. Pure-Python `Decimal`/`float` loops. | `grep` for `numpy\|pandas\|multiprocessing\|concurrent.futures` over `atlas/` → **zero hits** in compute paths |
| Process model | uvicorn is **one** process; the in-proc scheduler is one asyncio task. But the daily cycle does **not** run inside uvicorn: a daemon thread (`scheduler.py:119`) only launches and `wait()`-supervises a **separate OS subprocess** (`python -m atlas.ops.daily`, `scheduler.py:69`) that runs the cycle as one atomic DB transaction. **During a cycle window there are TWO Python processes and TWO connection pools**, which is precisely *why* a cycle crash rolls the day back atomically without taking the API down. The "single process / single engine" resource claim holds only *outside* the nightly cycle. | `atlas/api/main.py:43-46`, `atlas/ops/scheduler.py:69,119`, `atlas/ops/daily.py:702` |
| Database | **Single Postgres**, single engine, default `QueuePool` (5+10), `pool_pre_ping`. No replica, no partitioning, no query cache. | `atlas/core/db.py:18` |
| Caching | Feature-store `dataset_version` (content-addressed materialization pins, *not* a runtime cache); `functools.lru_cache` on calendars; per-request in-memory dicts. **Redis is provisioned but the application never connects to it.** | `atlas/dcp/features/store.py`, `atlas/core/config.py:9`, `atlas/tools/doctor.py:44` |
| Dominant bottleneck | **Per-name query loops** (screen/valuation N+1 over jsonb) and **serial per-instrument vendor fetch** in ingest. | `atlas/dcp/research/valuation_models.py:413`, `atlas/dcp/market_data/daily.py:247-266` |
| Headroom | Adequate *for one user, ~500 names, monthly rebalance.* Several O(N) paths (ingest, audit-chain rehash) grow unbounded and are already flagged (ADR-0016). | `docs/adr/0016-sp500-universe-expansion.md:71-73` |

**One-line verdict:** performance is "fast enough because the workload is tiny and the
cadence is daily/monthly," not because the system is engineered for throughput. The
architecture trades speed for *determinism, auditability, and point-in-time honesty* —
pure-Python engines, one transaction per day, full chain rehash every cycle. That is a
defensible trade for a paper research system, but it is **not** a foundation that scales to
minutes-granularity, multi-strategy, or multi-user without re-engineering.

---

## 1. What is measured vs what is not

**There is no performance test tier at all.** Concretely:

- `pyproject.toml` declares dev deps `pytest`, `pytest-cov`, `hypothesis`, `ruff`, `mypy` —
  **no `pytest-benchmark`, no `pytest-xdist`, no locust/k6**. `addopts = "-q"`.
- The only enforced quantitative gate on the codebase itself is **coverage on the risk
  engine only** (`make cov-risk` → 100% branch coverage on `atlas/dcp/risk`, `Makefile:23`).
  Global coverage is neither measured nor enforced.
- No timing assertions exist in the suite (I grepped for wall-time/elapsed assertions in
  `tests/` and found none that gate on duration).
- CI: `.github/workflows/ci.yml` **does** run the suite on every push/PR (`on: [push,
  pull_request]`; a `postgres:16` service so integration tests actually execute rather than
  skip; gated steps `ruff check atlas tests` → `mypy` → `pytest` → `alembic upgrade head`
  migration check). So the tests **do** run in CI — but the workflow runs **bare `pytest`
  with no timing capture, no perf-regression tracking, and no coverage gate** (it never
  invokes `make cov-risk`), so **suite runtime and every performance metric remain untracked
  over time.** The perf-tracking gap the rest of this document describes is real; the earlier
  drafts' "no CI at all" framing was **wrong** and has been corrected here — CI exists and
  gates lint/type/test/migration on push. (Whether the latest CI runs are green is a separate
  question this document does not verify.)

Everything in this document is therefore either an ad-hoc measurement or an estimate. **I am
stating this plainly because the review will (correctly) treat any unlabelled "X is fast"
claim as unsupported.**

---

## 2. Test suite runtime **[MEASURED — this session]**

I ran the full suite against the isolated `atlas_test` database, wrapped in `/usr/bin/time`:

```
python -m pytest -q          → 1515 passed
real 74.63s   user 32.72s   sys 5.31s
```

- **1515 tests collected** (verified: `pytest --collect-only` → "1515 tests collected in
  0.77s"). Ground-truth breakdown: unit 754 / integration 625 / constitution 75 (≈ counts;
  parametrization lifts the total to 1515). Grep of `def test_` across `tests/` → 1454
  function definitions across 207 files.
- **Wall 74.6s but CPU only ~38s (user+sys).** The ~37s gap is **I/O wait on serial Postgres
  round-trips** — the suite is single-process (no xdist) and heavily integration-DB-bound
  (625 integration tests each opening sessions, running migrations-adjacent fixtures, and
  hitting real SQL). This is the clearest single piece of evidence that the system is
  **DB-round-trip-bound and serial**, not CPU-bound.
- Prior observation in the ground-truth notes was **~65s**; I measured **74.6s** on a
  9-day-warm, otherwise-loaded machine. The variance (±15%) between two single runs on the
  same box is itself the reason to distrust any of these numbers to better than
  order-of-magnitude. **[cross-doc note flagged in §13.]**
- **Collection alone: 0.77s** for 1515 tests — import graph is cheap; the cost is entirely
  in execution against the DB.

**Implication:** even a modest `pytest-xdist -n auto` would likely cut wall time roughly in
half (the CPU/wall gap is recoverable parallelism), but it is not wired up, and the
integration tests share a single `atlas_test` database (`tests/integration/conftest.py`),
so naive parallelism would need per-worker schemas first. This is unbuilt.

---

## 3. Daily operating cycle (T0–T9b) wall time **[STRUCTURAL + ESTIMATED]**

The nightly cycle is the system's heartbeat: one `WorkflowRunner` graph, **17 nodes**, run
in **one atomic checkpointed transaction per calendar day** (`atlas/ops/daily.py:657-678`,
docstring `:86-97`). It fires once/day at 23:30 UTC via the in-process scheduler. I did
**not** benchmark a full live cycle end-to-end (it needs a live EODHD key for T0 ingest and
an Anthropic key for T7 desk), so the per-node figures below are **estimates** built from
the measured cost of the pieces I *could* run in isolation (§4–§5) plus the structural
shape of each node.

```mermaid
sequenceDiagram
    participant Sched as In-proc scheduler (asyncio task, in uvicorn)
    participant Cyc as Daily cycle (SEPARATE subprocess: python -m atlas.ops.daily, own engine, 1 DB txn)
    participant PG as Postgres (single)
    participant EODHD as EODHD vendor (per-instrument HTTP)
    participant LLM as Anthropic (sequential per name)

    Sched->>Cyc: Popen subprocess for daily-<date> (23:30 UTC), then wait()
    Cyc->>PG: t0 ingest — LOOP 511 US names
    loop per instrument (serial)
        Cyc->>EODHD: fetch_splits + fetch_bars (2 calls)
        EODHD-->>Cyc: bars
        Cyc->>PG: upsert_bar
    end
    Cyc->>PG: t1 verify — REHASH entire audit chain (ORDER BY seq)
    Cyc->>PG: t2..t6 expire/settle/stops/snapshot/bands/cusum/reconcile (cheap SQL)
    Cyc->>PG: t6b/t6c signal generation (xsmom + PEAD)
    Cyc->>LLM: t7 desk — LOOP shortlist names (serial)
    loop per shortlisted name (serial, budget-capped)
        Cyc->>LLM: bull+bear debate → specialists → CIO memo
    end
    Cyc->>PG: t8 bridge, t8b attribution, t8c core
    Cyc->>PG: t9 scorecard/learning/picks/screen-cohort, t9b brief
    Cyc-->>Sched: commit (or roll the whole day back atomically)
```

**Estimated per-node cost profile (live mode, 511 US names):**

| Node | Work | Cost driver | Est. wall |
|---|---|---|---|
| **t0 ingest** | Serial loop over 511 instruments, **2 vendor HTTP calls each** (`daily.py:247-266`) | **Network latency × ~1,022 round trips** — dominant node in live mode | **tens of seconds → minutes** (network-bound) |
| t1 verify_chain | Full re-hash of `audit.decision_events` `ORDER BY seq` (`audit_repo.py:52-63`) | O(total audit events), Python per-row sha256 | sub-second today (1,884 events); **grows unbounded** |
| t2–t6 | expire/settle/stops/snapshot/bands/cusum/reconcile — small bounded SQL | book size (a handful of positions) | milliseconds each |
| t6b/t6c | signal generation over the PIT panel | monthly rebalance day only | sub-second (cached eligible sets) |
| **t7 desk** | Serial loop over shortlist (~5–12 names), each = debate + specialists + CIO | **LLM latency × sequential names**, budget-capped | **the slow node in wall-clock when a key is set — minutes** |
| t8/t8b/t8c | bridge + attribution + core maintenance | book size | sub-second |
| t9/t9b | scorecard, learning, source-picks, screen cohort, brief | mostly bounded reads; the monthly screen-cohort path invokes the screen (§4) | sub-second, except cohort months |

**Honest characterization:** in **fixture/keyless mode** (how it runs in tests and demos)
the whole cycle is **DB-bound and completes in low single-digit seconds** — the adapter
reads local fixture files instead of hitting the network, and there is no LLM call. In
**live mode** the cycle is dominated by **(a) ~1,022 serial vendor round-trips at T0** and
**(b) sequential LLM calls at T7** — both are latency-bound, not CPU-bound, and both scale
linearly with universe/shortlist size. The ADR-0016 S&P-500 expansion explicitly flagged
that "nightly ingest wall time grows ~4–5×" and called it "another argument for the Linux
box" (`docs/adr/0016-sp500-universe-expansion.md:71-73`).

**Structural risk of the one-transaction-per-day design:** the entire cycle holds a single
DB transaction open for its full duration (`daily.py:86-97`). On a slow-network night, T0
alone can keep that transaction open for minutes while it does serial HTTP — a long-lived
write transaction on a single Postgres. For one user at daily cadence this is fine; it does
not generalize.

---

## 4. Opportunity screen & per-name valuation **[MEASURED — this session]**

The ground-truth note describes the screen as **"~14s / 506 names."** My live measurement
tells a more precise (and partly *different*) story, which I flag as a discrepancy in §13.

```
screen_opportunities(top_n=25)   → 4.862 s   (universe_n = 107, ranked_n = 107)
  ├─ _universe_fundamentals       → 0.579 s   (107 names carry a fundamentals snapshot ≤ as_of)
  └─ _universe_momentum           → 0.101 s   (487 names, one bounded-window query)
  remainder ≈ 4.18 s = 25 × per-name enrichment (valuation + models + autopsy)

compute_valuation(AAPL)           → 0.167 s
  └─ _peer_multiples(AAPL, IT)    → 0.153 s   (23 sector peers, per-peer jsonb multiple extraction)
```

Two structurally important facts fall out of this:

1. **The screen's *enriched* universe is ~107 names, not 506.** `_universe_fundamentals`
   (`health_score.py:109-146`) does an **INNER `JOIN LATERAL`** onto `market.fundamentals`,
   so any active name **without** a fundamentals snapshot dated ≤ `as_of` is dropped from
   the screen entirely. Today only ~107 US names satisfy that. The **"506 names" figure
   describes the deterministic *scanner's* breadth (§5), not the screen.** The screen's own
   docstring is accurate — "One universe query ranks all names… only the top-N are
   enriched" (`opportunity_screen.py:14-16`).

2. **The per-name valuation is a classic N+1 / O(N×sector) loop — this is the screen's real
   cost.** For each of the top-25 names, `compute_valuation` re-issues `_peer_multiples`
   (`valuation_models.py:413-444`), which does a **fresh `JOIN LATERAL` over every other
   active name in that GICS sector** and parses each peer's jsonb payload in Python. Measured
   at **~0.15s/name**, 25 names ≈ ~3.8s — i.e. the bulk of the 4.86s. There is **no batching,
   no memoization of sector-peer payloads across names**, and no shared per-sector cache, so
   two IT names each independently re-scan and re-parse the same ~23 IT peers.

**Why my 4.9s ≠ the noted 14s:** most plausibly (a) my DB page cache was warm (container up
9 days), (b) the current fundamentals coverage is thin (~107 names → smaller sectors → fewer
peer rows per name), and (c) the 14s may have been a colder run or a run with broader
fundamentals coverage. **Both numbers are single-sample observations; neither is a
benchmark.** The stable, code-level truth is the *shape*: whole-universe ranking is two batch
queries (cheap), and the per-name enrichment is a repeated per-sector jsonb scan (the thing
that grows).

The screen runs as a **background daemon thread** behind a non-blocking lock (one screen at a
time; "busy" is an honest answer) — `atlas/ops/screen.py:50-76`. It is deterministic, does
**zero model spend**, and is **measured-never-applied** (reaches no capital).

---

## 5. Deterministic scanner sweep **[MEASURED — this session]**

The scanner is the *true* whole-universe pass (ADR-0007), and it is **fast because it is
batch SQL, not a per-name loop**:

```
scan(top_n=5)  → 0.739 s   (scanned = 512, eligible = 510, ineligible = 2)
```

`atlas/dcp/scanner/v1.py` issues **one `_BARS_SQL` per market** (`:100-109`) that pulls the
last 60 vendor bars for *all* instruments in a single windowed query (`row_number() OVER
(PARTITION BY i.id …)`), plus one splits query per market, then scores in memory. Over
**2.47M stored bars** it returns in **~0.74s warm**. This is the model the rest of the
research plane should follow and mostly does not (§4).

Note the scanner *does* apply split-adjustment on read in Python for every eligible series
(`v1.py:302-311`) — pure-Python `Decimal` arithmetic over ~510 × 60 bars. That work is
included in the 0.74s and is not currently a bottleneck at this universe size.

---

## 6. Backtest gauntlet & recipe runs **[STRUCTURAL + OBSERVED/ESTIMATED]**

The heavy compute lives here, and it is **pure Python, single-threaded, sequential**.

- **No `numpy`, no `pandas` anywhere in the codebase.** The portfolio backtester is `math` +
  `statistics` + `bisect` + `Decimal`/`float` over aligned panels (`portfolio.py` header:
  *"Deterministic, pure, no DB access"*, imports at `:33-37`).
- **The 1000-path monkey null runs in a plain Python `for` loop, one path at a time:**
  `portfolio_validation.py:84-85` (`for _ in range(paths): run_portfolio_backtest(...)`),
  and identically in `pead_pit_run.py:275-276`, `recipe_run.py:231-232`. Each path replays a
  full monthly-rebalanced backtest over the PIT S&P-500 panel (2012→). **No vectorization,
  no parallelism, no shared work across paths** beyond a per-rebalance eligible-set cache
  (`recipe_run.py`, `portfolio_validation.py:70-76`).
- The **recipe gauntlet** (`atlas/dcp/factory/recipe_run.py:311`) is the most expensive
  single operation the system runs on demand. One `run_recipe` executes: the main strategy
  backtest **+ a 1000-path null + deflated Sharpe + a purged/embargoed walk-forward (k folds)
  + a pre-committed kill-leg (a *second* full registration with its own 1000-path null)**.
  That is effectively **~2 × (1 backtest + 1000 null paths + k walk-forward folds)** over a
  ~13-year, ~500-name panel, pure-Python.

**Timing:** the ground-truth estimate is **~8–12 minutes per recipe gauntlet
[OBSERVED/ESTIMATED]**. I did not re-run it in this session (it is minutes-long and needs the
full PIT panel loaded), so I neither confirm nor refute the exact figure — but the structure
(2 × 1000 sequential pure-Python portfolio replays over 13y×500 names) is entirely consistent
with a single-digit-to-low-double-digit-minute wall time. Recipe runs execute in a
**background daemon thread behind a non-blocking lock** (`atlas/ops/recipes.py`, "one at a
time; busy is an answer"), so the API stays responsive while a gauntlet churns.

**This is the single biggest latent CPU cost in the system**, and it is the most obviously
parallelizable (1000 independent seeded paths → embarrassingly parallel), yet it is fully
serial. `numpy` vectorization of the portfolio engine and/or a `ProcessPoolExecutor` over
null paths would likely be 1–2 orders of magnitude faster; neither is built.

---

## 7. Bottleneck inventory (ranked)

| # | Bottleneck | Where | Character | Scales with | Mitigated? |
|---|---|---|---|---|---|
| 1 | **Serial per-instrument vendor fetch** in nightly ingest (2 HTTP calls × 511 names) | `market_data/daily.py:247-266` | Network-latency-bound, serial | universe size (linear) | No. ADR-0016 flagged ~4–5× growth. |
| 2 | **Sequential LLM desk** — names processed one at a time, each = debate+specialists+CIO | `agents/desk.py:115-163` | LLM-latency-bound, serial; budget-capped | shortlist size | Partly — budget breaker caps spend, not latency |
| 3 | **1000-path null + kill-leg, pure-Python serial** | `backtest/portfolio_validation.py:84`, `factory/recipe_run.py:231` | CPU-bound, embarrassingly parallel but serial | paths × panel length × universe | No |
| 4 | **Per-name valuation N+1 over jsonb** (per-sector re-scan per name) | `research/valuation_models.py:413` | DB + Python jsonb parse, repeated | top_n × sector size | No batching/memoization |
| 5 | **Full audit-chain rehash every cycle** (`ORDER BY seq`, per-row sha256) | `core/audit_repo.py:52-63` | O(total events), runs nightly | lifetime event count (unbounded) | No incremental verify |
| 6 | **Single Postgres, single process, one txn/day** | `core/db.py:18`, `ops/daily.py:86` | Serialization point; long-lived write txn | everything | No; Linux-box migration deferred |
| 7 | **Audit writes serialized by advisory lock** | `core/audit_repo.py:29` | Global lock per append | write concurrency | Intentional (chain integrity) |

Bottlenecks #1–#3 are latency/CPU and dominate *wall time* in live mode. #4–#5 are the ones
that **grow silently** and will bite later without a code change.

---

## 8. Memory & CPU profile **[STRUCTURAL]**

- **Memory:** modest and bounded by universe size, not history. The heavy queries pull
  bounded windows (scanner: last 60 bars/name; momentum: one bounded window; screen: latest
  fundamentals only), so the resident set for a scan/screen is O(active names × small window)
  — megabytes, not gigabytes. The backtest loads the PIT panel (≈500 names × ~13y daily
  closes+opens) into Python objects; that is the largest single in-memory structure and is
  still comfortably within a laptop's RAM (tens–low-hundreds of MB depending on
  representation). **No memory profiling has been done; there are no memory ceilings or
  guards.** [ASSUMPTION, not verified: I did not measure RSS.]
- **CPU:** single-core-bound. The suite showed **~38s CPU / 74.6s wall** — i.e. the busiest
  the system gets in normal operation is *half* a core, the rest is I/O wait. The one path
  that genuinely saturates a core for minutes is the recipe gauntlet (§6), and even that
  uses exactly **one** core.
- **No async DB.** Sessions are synchronous SQLAlchemy (`core/db.py`); the FastAPI endpoints
  are largely sync handlers doing blocking DB I/O. Under concurrent requests, blocking
  handlers would tie up the event loop's threadpool — not a problem at N=1 user, a real one
  at scale.

---

## 9. Parallelism & concurrency model **[STRUCTURAL]**

**There is no compute parallelism.** What concurrency exists is for *responsiveness*, not
*speed*:

```mermaid
flowchart TD
    subgraph proc["uvicorn process (port 8001) — own engine + QueuePool 5+10"]
        API["FastAPI sync handlers<br/>blocking DB I/O"]
        SCHED["asyncio scheduler task<br/>(ATLAS_INPROC_SCHEDULER=1)"]
        subgraph workers["In-process daemon threads (one job per surface)"]
            SCR["screen worker<br/>non-blocking lock"]
            ANA["analyze/desk worker<br/>non-blocking lock"]
            REC["recipe gauntlet worker<br/>non-blocking lock"]
            SUP["cycle SUPERVISOR thread<br/>(scheduler.py:119) — only Popen + wait()"]
        end
    end
    subgraph cycproc["SEPARATE OS subprocess: python -m atlas.ops.daily — its OWN engine + QueuePool"]
        CYC["daily cycle<br/>ONE atomic DB txn"]
    end
    API --> PG[("Single Postgres<br/>(each process opens its own pool)")]
    SCR --> PG
    ANA --> PG
    REC --> PG
    CYC --> PG
    SCHED -. start_cycle .-> SUP
    SUP -. Popen + wait .-> CYC
    REDIS[("Redis container<br/>PROVISIONED, UNUSED")]:::unused
    classDef unused fill:#fee,stroke:#c33,stroke-dasharray: 5 5;
```

- **Single uvicorn worker.** `Makefile:26` launches `uvicorn atlas.api.main:app --port 8001`
  with **no `--workers`** — one process. The in-process scheduler is a single asyncio task
  (`api/main.py:43-46`).
- **Background jobs are single daemon threads guarded by a non-blocking `threading.Lock`** —
  screen (`ops/screen.py:68`), analyze/desk (`ops/analyze.py:231`), recipes
  (`ops/recipes.py:234`), and pick-ingest (`ops/ingest_picks.py:183`) all run their work
  *in-process*. The daily-cycle daemon thread (`ops/scheduler.py:119`) is the **exception**: it
  holds the same non-blocking lock, but its body is only a
  `subprocess.Popen([sys.executable, "-m", "atlas.ops.daily"])` + `proc.wait()`
  (`scheduler.py:69-92`) — the cycle work itself executes in that *child process*, not the
  thread. The pattern is deliberate: **"one at a time; busy is an answer, not an error."** This
  gives the console a responsive UI (kick off a long job, poll status) but means, e.g., two
  screens or two gauntlets cannot run concurrently — by design.
- **The daily cycle runs as a separate OS subprocess, not a thread inside uvicorn.** The daemon
  thread only launches and `wait()`-supervises `python -m atlas.ops.daily`
  (`scheduler.py:69,119`); that child imports `atlas.core.db` fresh and builds its **own**
  SQLAlchemy engine + QueuePool (`daily.py:702` → `db.py:18`), then runs the whole cycle in one
  atomic DB transaction. So a cycle window is **two processes with two connection pools**, and
  the subprocess boundary is exactly *why* a cycle crash (even a segfault, per `scheduler.py:46`)
  rolls the day back atomically without taking the API down; a re-run replays from T0
  deterministically (`ops/daily.py:86-97`). Correctness-first, throughput-last.
- **The LLM desk is sequential across names** (`agents/desk.py:115`), each name running
  debate → specialists → CIO in order, with `SCHEMA_MAX_ATTEMPTS=3` retries per role on cage
  failure (so a stubborn name can cost up to 3× its LLM calls before the run holds). No
  fan-out across names or roles.

**Net:** the system is a **single-writer, single-core, one-job-per-surface** design. This is
coherent with its goals (determinism, atomicity, auditability) and its scale (one user), but
it is the opposite of a throughput architecture.

---

## 10. Caching **[STRUCTURAL + MEASURED]**

There are four caching-shaped things in the system; **none of them is Redis.**

1. **Feature store `dataset_version` (content-addressed materialization pins).**
   `atlas/dcp/features/store.py` — `dataset_version = sha256(feature, version, input-extent)`.
   This is **not a runtime read cache**; it is a *correctness* mechanism that makes
   re-materializing the same inputs a **no-op** (`ON CONFLICT DO NOTHING` on the natural key)
   and forces any new/backfilled input to a **new version** rather than silently reusing a
   stale value (`store.py:36-77`). So it *avoids recompute* across runs (a real speed benefit
   for backtests that pin a panel) but its primary purpose is point-in-time integrity, not
   latency. The read path is index-supported (`feature_values_read_idx`, migration 0024).
2. **`functools.lru_cache` on exchange calendars** (`market_data/calendars.py:25`,
   `maxsize=None`) — cheap, correct, the calendar is small and mostly static.
3. **Per-request in-memory dicts** — `fx_cache`/`px_cache` in attribution
   (`reporting/attribution.py:239-252`), `fx_cache` in the bridge (`trading/bridge.py:308`)
   and bands (`trading/bands.py:269`), and per-rebalance eligible-set caches in every backtest
   runner (`xsmom_pit_run.py:355`, `portfolio_validation.py:70`, etc.). These are
   **within-call** memoizations that avoid re-querying FX/prices/eligibility for the same key
   inside one operation. They do **not** persist across requests.
4. **No query-result cache, no HTTP response cache, no cross-request memoization.**

**Redis: provisioned but unused.** `redis_url = "redis://localhost:6379/0"` is a config
default (`core/config.py:9`) and `docker compose up -d db redis` starts a Redis container
(`tools/doctor.py:44`) — and I confirmed the container is **Up 9 days** this session. But a
repo-wide grep for `import redis` / `redis.Redis` / `aioredis` / cache-client usage returns
**no application code that ever connects to it.** Redis is dead weight in the current design;
either wire it up (a cross-request cache for the screen/valuation N+1, or a job queue to
replace the daemon-thread pattern) or drop it from the compose file. **This is a real, if
minor, tech-debt / honesty flag.**

**Notably absent:** the biggest cache win available — memoizing **sector-peer fundamentals
payloads across the top-N names in one screen run** (§4) — is not done, even though those
payloads are re-fetched and re-parsed once per name within the *same* call.

---

## 11. Database performance **[STRUCTURAL + MEASURED]**

**Single Postgres — but one engine *per process*.** `atlas/core/db.py:18` creates one engine
with `pool_pre_ping=True` and otherwise **default SQLAlchemy `QueuePool` (size 5, max_overflow
10)** — no explicit pool tuning, no read replica, no connection multiplexing (PgBouncer),
no partitioning, no table clustering. `expire_on_commit=False`. The `_session_factory` is a
module-global memoized *within a single process* (`db.py:12-20`), **not** shared across
processes: the nightly cycle subprocess (§9) therefore builds a **second** engine + QueuePool
against the same Postgres, so a cycle window holds up to 2×(5+10) connections across two pools,
not one. The database is single; the "single engine" framing is true only for the uvicorn
process in isolation.

**Index landscape.** 34 migrations create **19 explicit `CREATE INDEX` statements**; the hot
paths mostly ride **primary keys / unique constraints** rather than secondary indexes:

| Hot table | Rows (this session) | Access pattern | Index support | Gap |
|---|---|---|---|---|
| `market.price_bars_daily` | **2,466,700** | windowed `PARTITION BY instrument_id ORDER BY bar_date DESC`, `WHERE source=… AND close IS NOT NULL AND bar_date ≤ cutoff` | **PK `(instrument_id, bar_date)`** (`0001_initial.py:46`) supports the partition/range | `source` & `close IS NOT NULL` are **post-index filters** (no partial/covering index); acceptable at this size, measured scanner 0.74s |
| `market.fundamentals` | 526 | `JOIN LATERAL … WHERE instrument_id=i.id AND as_of ≤ :on ORDER BY as_of DESC LIMIT 1` | **UNIQUE `(instrument_id, as_of)`** (`0012_fundamentals.py:37`) supports the latest-≤ lookup | jsonb payload parsed in Python per row (§4) — the cost is CPU parse, not the index |
| `quant.feature_values` | (panel-dependent) | `feature_at`/`feature_panel` range scans | **PK + `feature_values_read_idx (feature_id, dataset_version, instrument_id, session_date)`** (`0024`) | well-indexed for the documented read path |
| `audit.decision_events` | 1,884 | append (advisory-locked) + **full `ORDER BY seq` rehash every cycle** | **PK `(id)` + UNIQUE `seq`** (`0001_initial.py:96-108`) | no index on `event_type`/`entity_id` → any *filtered* audit query seq-scans; the nightly **full rehash is O(total)** and **unbounded** |
| `trading.*` | small | state-filtered lookups | `trade_proposals_state_expires_idx`, `orders_state_idx`, `positions_one_open_per_instrument` (unique), `tax_lots_position_idx` | adequate for book size |

**The two DB scaling concerns worth naming to the committee:**

1. **`price_bars_daily` at 2.47M rows and growing daily.** The PK covers the per-instrument
   windowed reads well (measured fast). But it is a single unpartitioned table; at
   multi-year × multi-thousand-name scale it would benefit from range partitioning by
   `bar_date` or `instrument_id`. Not done; not needed yet.
2. **`audit.decision_events` full rehash every T1 (`audit_repo.py:52-63`).** Every daily
   cycle reads the **entire** chain ordered by `seq` and re-hashes it in Python. Today that
   is 1,884 events (sub-second). It grows **without bound** as the fund operates — every
   material action appends an event, and nothing ever prunes (append-only is an invariant).
   In a year of daily operation this is still small; over the system's life it becomes an
   O(n) nightly cost with no incremental-verification fast path. **Flagging it now because it
   is invisible at current scale and structurally unbounded.**

---

## 12. Live measurement appendix **[MEASURED — this session, reproducible]**

All figures single-run, warm cache, dev DB `atlas`. Reproduce with the snippets below.

| Probe | Result |
|---|---|
| Full test suite (`atlas_test` DB) | **real 74.63s / user 32.72s / sys 5.31s**, 1515 passed |
| `pytest --collect-only` | 1515 tests collected in **0.77s** |
| Scanner `scan(top_n=5)` over full universe | **0.739s** (scanned 512, eligible 510) |
| `screen_opportunities(top_n=25)` | **4.862s** (universe_n 107) |
| ├ `_universe_fundamentals` | 0.579s (107 names) |
| ├ `_universe_momentum` | 0.101s (487 names) |
| `compute_valuation(AAPL)` | 0.167s |
| └ `_peer_multiples(AAPL, IT)` | 0.153s (23 peers) |
| Row counts | instruments 708 · price_bars_daily **2,466,700** · fundamentals 526 · decision_events 1,884 · active US 511 |

Reproduce the research-plane timings:

```python
import time
from atlas.core.db import session_scope
from atlas.core.clock import FrozenClock
from datetime import datetime, UTC
from sqlalchemy import text
from atlas.dcp.scanner.v1 import scan
from atlas.dcp.research.opportunity_screen import screen_opportunities

with session_scope() as s:
    as_of = s.execute(text("select max(bar_date) from market.price_bars_daily")).scalar()
    clk = FrozenClock(datetime(as_of.year, as_of.month, as_of.day, 23, 30, tzinfo=UTC))
    t = time.perf_counter(); scan(s, clk, top_n=5);            print("scan", time.perf_counter()-t)
    t = time.perf_counter(); screen_opportunities(s, as_of);   print("screen", time.perf_counter()-t)
```

Reproduce the suite timing:

```bash
export ATLAS_DATABASE_URL="postgresql+psycopg://atlas:atlas_local_only@localhost:5432/atlas_test"
/usr/bin/time -p python -m pytest -q
```

---

## 13. Cross-document inconsistencies noticed

1. **"Opportunity screen ~14s / 506 names" vs measured ~4.9s over ~107 names.** The
   ground-truth figure conflates two different things: **506** is the *scanner's* breadth
   (I measured the scanner at 512 scanned / 510 eligible in **0.74s**), whereas the **screen**
   only *enriches* names that carry a fundamentals snapshot — currently ~107 due to the INNER
   `JOIN LATERAL` in `_universe_fundamentals` (`health_score.py:109-146`) — and I measured it
   at **4.86s warm**. Neither "14s" nor "4.9s" is a benchmark (single samples, cache-sensitive),
   but the *attribution* of "506 names" to the screen is imprecise; the screen never touches
   506 names. Recommend the review treat the screen cost as "**O(top_n) per-sector jsonb
   re-scans, single-digit seconds warm**," and the scanner as "**sub-second whole-universe
   batch sweep**."
2. **"Full suite ~65s" vs measured 74.6s.** Within run-to-run variance on a loaded laptop;
   not a contradiction, but a reminder that no figure here is repeatable to better than
   ±~15%.

---

## 14. Future scaling **[ASSESSMENT]**

What would have to change, roughly in priority order, if this ever left "one user / ~500
names / daily-monthly cadence":

1. **Move off the MacBook to a Linux box** (already recommended and **deferred** by the
   Principal). The current host also has launchd supervision dead (macOS TCC) and iCloud
   conflict-copy risk on `~/Documents` — those are reliability, not throughput, but they gate
   any "run it seriously" story (see Doc 16).
2. **Parallelize the null model / recipe gauntlet** — `ProcessPoolExecutor` over the 1000
   independent seeded paths, and/or **replace the pure-Python portfolio engine with a
   vectorized (numpy) core**. Biggest single CPU win available; embarrassingly parallel.
3. **Batch/parallelize ingest** — the serial per-instrument 2-HTTP-call loop is the nightly
   wall-time driver in live mode and grows linearly; EODHD bulk endpoints and/or a bounded
   thread pool would cut it sharply.
4. **Fix the screen N+1** — memoize sector-peer payloads across names in one run (a
   per-sector cache), or compute all peer medians in one universe pass. This is the obvious
   Redis use-case that Redis is currently *not* used for.
5. **Incremental audit-chain verification** — checkpoint a verified `(seq, hash)` and verify
   only the tail, so T1 stops being O(lifetime events).
6. **DB scaling** — partition `price_bars_daily`; add a read replica for the console's read
   traffic; tune the pool; PgBouncer if the process model ever goes multi-worker.
7. **Test parallelism** — `pytest-xdist` with per-worker schemas would roughly halve suite
   wall time (the CPU/wall gap is recoverable), and the existing CI — which today runs bare
   `pytest` with no timing capture — could then track it.

None of the above is built. All are single-machine-appropriate deferrals for a paper system,
and I am **not** implying any of it is in progress.

---

## 15. Weaknesses / Debt / Open (performance-specific)

- **No performance test tier of any kind.** No benchmarks, no load/stress tests, no latency
  budgets, no regression tracking. Every number in this doc is ad-hoc. *(This is the headline
  weakness.)*
- **Single everything:** one machine, one Postgres, one core for compute, one job per surface,
  one transaction per day. (Process count is the one *exception* to "single": the nightly cycle
  runs as a **separate subprocess** with its own engine/pool — §9/§11 — so a cycle window is two
  processes, not one. That is a deliberate crash-isolation choice, not a scaling feature.)
  Coherent for N=1; not a throughput design.
- **Pure-Python compute, no numpy/pandas.** The 1000-path nulls and portfolio replays are
  serial Python `Decimal`/`float` loops — the largest latent CPU cost, fully unparallelized.
- **Two O(N)-in-universe serial loops** (per-instrument ingest fetch; per-name valuation
  N+1) that ADR-0016 already flagged as ~4–5× growth from the S&P-500 expansion.
- **One unbounded O(lifetime) nightly cost:** full audit-chain rehash every T1. Invisible
  today (1,884 events), structurally unbounded.
- **Redis provisioned but never connected** — a running container doing nothing; either wire
  it (the screen N+1 and the daemon-thread job pattern both want it) or delete it.
- **Long-lived single write transaction per cycle** that can stay open for minutes during
  network-bound ingest.
- **Sync DB / sync handlers under a single uvicorn worker** — fine for one console user,
  would serialize under concurrency.
- **Measurement honesty:** figures are single-sample and cache-sensitive; the "14s/506"
  screen claim is imprecise (§13); "~65s" suite vs measured 74.6s shows the ±15% run-to-run
  noise. Do not quote any of these as SLAs.
- **No profiling has ever been done** (no flame graphs, no `EXPLAIN ANALYZE` captured in the
  repo, no memory RSS measurement). The bottleneck ranking in §7 is reasoned from code +
  spot measurements, not from a profiler.
