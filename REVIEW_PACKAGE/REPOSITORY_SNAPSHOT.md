# REPOSITORY_SNAPSHOT — the exact version this evidence pass describes

> **Purpose.** Every independent reviewer must be able to identify *exactly* which repository
> version, runtime, and command set this documentation describes. Nothing in the evidence-pass
> files (`EVIDENCE_BASE.md`, `EVIDENCE_INDEX.md`, `BACKTEST_REPRODUCIBILITY.md`,
> `STRATEGY_IMPLEMENTATION_TRACEABILITY.md`, `POINT_IN_TIME_AND_BIAS_ANALYSIS.md`,
> `PERFORMANCE_EVIDENCE.md`, `STATISTICAL_VALIDATION_GAPS.md`, `QUANTITATIVE_ASSUMPTION_REGISTER.md`,
> `BASELINE_COMPARISON_REQUIREMENTS.md`, `EXECUTION_SAFETY_REVIEW.md`, `AI_AGENT_CONTROL_MODEL.md`,
> `DOCUMENTATION_CODE_DISCREPANCIES.md`, `REMEDIATION_BACKLOG.md`) is valid for any other commit.
>
> This file, and every file listed above, was produced **without modifying application code, tests,
> configuration, infrastructure, or datasets** — only files inside `REVIEW_PACKAGE/` were created or
> changed. This is an *evidence and reproducibility* pass layered on top of the already-reviewed
> package (`00`–`20` + `README`); it does **not** regenerate that package and does **not** repeat the
> architectural review.

## 1. Repository identity

| Field | Value |
|---|---|
| Remote | `github.com/jkaryampudi/atlas` (private) |
| Branch | `main` |
| Commit (full) | `2ba38c0d0a94cb8d518cc604766157f45a9ac6da` |
| Commit (short) | `2ba38c0` |
| Commit subject | `REVIEW_PACKAGE: second-pass Citadel/Two-Sigma-PE hardening — the damning catches` |
| Commit datetime | `2026-07-20T18:32:27+10:00` (AEST) |
| Working tree | **clean** — 0 uncommitted, 0 untracked files at review time |
| Sync with origin | 0 ahead / 0 behind `origin/main` |
| Review datetime (UTC) | `2026-07-20T08:41:38Z` |

**Note on `.coverage`.** Running the risk-engine coverage probe (§4, EV-07) regenerates the tracked
`.coverage` SQLite artifact. It was restored (`git checkout -- .coverage`) immediately after the run
so the working tree faithfully matches `2ba38c0`. No source, test, config, or data file was touched.

## 2. Runtime environment (verified — EV-02)

| Component | Version | How obtained |
|---|---|---|
| OS / kernel | Darwin 24.6.0, arm64 (Apple Silicon) | `uname -mrs` |
| Python | 3.14.4 | `.venv/bin/python --version` |
| PostgreSQL (server) | 16.14 (Debian 16.14-1.pgdg13+1), Docker | `SHOW server_version` |
| Deployment | single machine, single process, Docker Postgres (per `README`/`16`) | — |

### Python dependency versions (from the active `.venv`)
```
fastapi==0.139.0          pydantic==2.13.4          pydantic-settings==2.14.2
sqlalchemy==2.0.51        alembic==1.18.5           psycopg==3.3.4
httpx==0.28.1             uvicorn==0.51.0           pytest==9.1.1
hypothesis==6.156.6       ruff==0.15.21             mypy==2.2.0
numpy==2.5.1              pandas==3.0.3             streamlit==1.59.1
exchange-calendars==4.13.2
anthropic: NOT INSTALLED
```

**Load-bearing finding:** the `anthropic` SDK is **not installed**. The LLM transport is raw `httpx`
(`atlas/agents/runtime/llm.py:7` `import httpx`; `class AnthropicClient` at line 53). Any package
document that says the system uses the "Anthropic SDK" describes intent, not code. See
`DOCUMENTATION_CODE_DISCREPANCIES.md` (D-01) and `AI_AGENT_CONTROL_MODEL.md`.

## 3. Commands executed during this pass (safe, local, read-only or test-DB only)

Each row is reproduced verbatim in `EVIDENCE_BASE.md` with its full output and classification.

| # | Command (abbreviated) | Scope | Result | Evidence |
|---|---|---|---|---|
| 1 | `pytest tests/unit/test_boundaries.py` | atlas_test DB | **2 passed** — two-plane wall holds | EV-03 |
| 2 | `python -m atlas.tools.verify_chain` | read-only | **audit chain OK: 1885 event(s) verified** | EV-04 |
| 3 | `pytest tests/unit/test_redteam_v1.py` | atlas_test DB | **9 passed** — no-agent-numbers wall | EV-05 |
| 4 | `deflated_sharpe(...)` direct call | pure function | probability in [0,1]; reproduces 0.999 @ n=1, 0.853 @ n=23 | EV-06 |
| 5 | recipe gauntlet `--dry-run` | no DB (asserted) | deterministic `spec_hash`, seed 7, 1000-path; **no database touched** | EV-08 |
| 6 | `pytest --cov=atlas.dcp.risk --cov-branch --cov-fail-under=100` | atlas_test DB | **100.00%** (483 stmts / 118 branches / 0 missing) | EV-07 |

All test-DB commands are isolated to the `atlas_test` database per the project's test harness; none
writes to the development database or the audit chain.

## 4. Commands deliberately NOT executed (and why)

| Command | Reason not run | Rubric status |
|---|---|---|
| `make replay DATE=…` | `atlas/dcp/market_data/replay.py` uses `session_scope()` (lines 13, 27) → writes the **development** DB and audit chain | **NOT EXECUTED — UNSAFE SIDE EFFECT** |
| Full historical gauntlet (the run behind the +737% headline) | Excessive runtime; and — critically — the historical run **cannot be connected to a pinned config + commit + data snapshot** from within this pass (see `BACKTEST_REPRODUCIBILITY.md`). Reproducing it here would *generate a new result*, not reproduce the cited one. | **NOT EXECUTED — MISSING DATA / NOT CONNECTABLE TO ORIGINAL RUN** |
| Any live-model / desk LLM run | `anthropic` SDK absent; no Anthropic API key present | **NOT EXECUTED — MISSING CREDENTIALS** |
| Live broker / order submission | No broker exists; paper-only | **NOT APPLICABLE — no live path built** |

## 5. Credentials and datasets NOT available to this pass

- **Anthropic API key** — not present; live LLM evals and desk runs cannot be executed or reproduced.
- **EODHD licensed price/dividend data** — single-vendor market data (`README`, `03`). Licensed;
  **must not** be uploaded to any external reviewer. Historical bars underpin every performance
  number but are not independently re-derivable here.
- **Broker / account data** — none exists (paper-only). No PII, no account numbers.
- **Secrets / `.env`** — plaintext local secrets exist per `14_SECURITY.md`; **must not** be shared.

> Per the task's non-negotiable constraint: do not recommend uploading secrets, `.env` files,
> licensed datasets, API keys, credentials, broker/account information, or PII to any reviewer.

## 6. What a reviewer can and cannot reproduce from this snapshot

- **Can reproduce now** (deterministic, local, no licensed data): the six commands in §3 — boundary
  wall, audit-chain integrity, red-team wall, `deflated_sharpe` values, recipe determinism, and the
  risk-engine 100% branch coverage.
- **Cannot reproduce from this snapshot**: the historical backtest performance figures (need the
  licensed EODHD snapshot **and** a recorded run→config→commit→data linkage that does not currently
  exist as a single reproducible artifact — see `BACKTEST_REPRODUCIBILITY.md` and
  `PERFORMANCE_EVIDENCE.md`), and any live-model behaviour (needs credentials).

This snapshot is the anchor for every classification in `EVIDENCE_BASE.md` and `EVIDENCE_INDEX.md`.
