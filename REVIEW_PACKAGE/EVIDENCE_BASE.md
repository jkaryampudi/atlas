# EVIDENCE_BASE — the classified, command-level evidence behind this pass

> Anchored to `REPOSITORY_SNAPSHOT.md` (commit `2ba38c0`, review `2026-07-20T08:41:38Z`).
> Every evidence-pass document cites these `EV-##` IDs so a reviewer can trace any claim back to a
> command output or an exact source line. `EVIDENCE_INDEX.md` is the flat claim→evidence table built
> on top of this file.

## Classification taxonomy (task-part 2)

Applied uniformly across every evidence-pass document. **The governing rule: code existence is not
verified behaviour.** A class, function, interface, config key, comment, mock, docstring, or ADR
that *describes* a behaviour earns at most **CLAIMED** or **NOT TESTED** — never **VERIFIED** —
until a command, test, or exact-line inspection confirms the behaviour actually holds.

| Tag | Meaning | Bar to earn it |
|---|---|---|
| **VERIFIED** | Confirmed true this pass | A command/test was executed and its output confirms it, **or** the exact source line that must hold was read and it holds. |
| **INFERRED** | Reasoned from code structure | The code path was traced by inspection and is consistent, but end-to-end behaviour was not executed this pass. |
| **CLAIMED** | Asserted, not independently confirmed | Stated in a doc / comment / README / ADR / commit message; not checked against execution this pass. |
| **NOT TESTED** | Code exists; behaviour unexercised | The implementation is present but no test or run exercised the end-to-end behaviour this pass. |
| **NOT FOUND** | Searched, absent | Looked for the artifact; it does not exist in the repo at this commit. |
| **PLANNED** | Explicitly future / unbuilt | The repo itself marks it as roadmap / Phase-N / TODO / unbuilt. |
| **UNKNOWN** | Undeterminable here | Cannot be established from the evidence available to this pass (e.g. needs licensed data or credentials). |

## A. Evidence VERIFIED by execution or exact-line inspection

### EV-01 — Repository baseline · VERIFIED (executed)
`git` reports branch `main`, commit `2ba38c0d0a94cb8d518cc604766157f45a9ac6da`, clean working tree
(0 changed after restoring the `.coverage` artifact), 0 ahead/0 behind `origin/main`. Full detail in
`REPOSITORY_SNAPSHOT.md` §1.

### EV-02 — Runtime & dependency versions · VERIFIED (executed)
Python 3.14.4, Darwin 24.6.0 arm64, PostgreSQL 16.14. Dependency versions listed in
`REPOSITORY_SNAPSHOT.md` §2. **`anthropic` SDK is NOT installed** — LLM transport is raw `httpx`
(`atlas/agents/runtime/llm.py:7`, `class AnthropicClient` at `:53`). This is a documentation/code
discrepancy wherever "Anthropic SDK" is claimed (see D-01).

### EV-03 — Two-plane wall (dcp ⊥ agents) · VERIFIED (executed)
`pytest tests/unit/test_boundaries.py` → **2 passed**. The import-boundary invariant (`atlas/dcp/**`
never imports `atlas/agents/**`; agents never import `atlas.dcp.risk`/`atlas.dcp.execution`) is
enforced by a passing test at this commit.

### EV-04 — Audit hash-chain integrity · VERIFIED (executed)
`python -m atlas.tools.verify_chain` → **`audit chain OK: 1885 event(s) verified`**. The append-only
`audit.decision_events` chain is intact and re-verifiable read-only over 1,885 events at this commit.

### EV-05 — No-agent-numbers red-team · VERIFIED (executed)
`pytest tests/unit/test_redteam_v1.py` → **9 passed**. The 9-test adversarial suite proving an LLM
cannot inject a sizing/pricing/execution number (a BUY without DCP evidence refs is a validation
error) passes at this commit.

### EV-06 — `deflated_sharpe` is a PROBABILITY, and it reproduces both headline DSR numbers · VERIFIED (executed)
Direct calls to `atlas.dcp.backtest.validation.deflated_sharpe(sr_annual, n_days, n_trials)`:
- `deflated_sharpe(0.82, 3400, 1)  = 0.9987` → reproduces the flagship's **approval DSR ≈ 0.999** at trial-count 1.
- `deflated_sharpe(0.82, 3400, 23) = 0.8532` → reproduces the **ADR-0016 "≈0.85, would-fail-today"** figure at the lineage-scoped trial count.
- `deflated_sharpe(-1.0, 3400, 1)  = 0.0001` → confirms the output is a **probability in [0,1]** (Bailey/López de Prado, normal-returns assumption), **not** a Sharpe ratio.
This independently confirms both the approval number and the grandfather-clause number are *computed
from the same function*, and that the DSR gate (≥ 0.90) is a probability threshold. The **normal-returns
assumption** is itself an unvalidated modeling choice (see `QUANTITATIVE_ASSUMPTION_REGISTER.md`).

### EV-07 — Risk engine 100% branch coverage · VERIFIED (executed)
`pytest --cov=atlas.dcp.risk --cov-branch --cov-fail-under=100` →
**`TOTAL 483 stmts / 118 branch / 0 miss / 100%`; "Required test coverage of 100% reached. Total
coverage: 100.00%"**. The `make cov-risk` claim in `01`/`07` is confirmed by execution. *Scope caveat:*
100% coverage is **on `atlas/dcp/risk` only**; it is line/branch execution coverage, not a proof of
correctness, and says nothing about the rest of the codebase (coverage is measured only here).

### EV-08 — Recipe gauntlet determinism (no side effects) · VERIFIED (executed)
Recipe gauntlet `--dry-run` produced a deterministic `spec_hash` (`99d193f1…`), confirmed seed **7**
and the **1000-path** monkey null, and asserted **no database was touched**. Establishes that the
gauntlet *spec* is content-addressed and reproducible-in-principle; it does **not** execute the full
backtest (see EV-11).

### EV-09 — Deterministic risk gate after AI, before fill · VERIFIED (executed + traced)
`pytest tests/unit/test_approval_recheck.py` → **5 passed**. Code trace: `atlas/dcp/trading/proposals.py`
`approve()` (`:912`) calls `recheck_at_approval` (imported `:119` from `atlas.dcp.risk.approval_recheck`);
a fresh FAIL yields `ApprovalOutcome(status="RISK_RECHECK_FAILED", …)` (`:881`, `:975`), which the API
(`atlas/api/routers/trading.py:145` `approve_proposal`) surfaces as a 409 that **voids the approval,
terminal**. The order/fill path is reached **only** after this deterministic re-check passes. No LLM is
in this path. Detailed in `EXECUTION_SAFETY_REVIEW.md`.

## B. Evidence traced by inspection (not executed end-to-end this pass)

### EV-13 — Only order path is `PaperBroker.submit` · INFERRED (traced)
`atlas/dcp/execution/paper.py`: `class PaperBroker` (`:135`), `submit(...)` (`:143`); the module
docstring states "Phase 7 live brokers implement the same protocol behind the arming gate" (`:72`).
No live broker class exists (see EV-16 NOT FOUND). Order creation is reached from `proposals.approve()`
after EV-09's re-check. End-to-end fill behaviour is covered by the package's paper-trading tests but
was **not** re-executed in this pass.

### EV-14 — No-look-ahead is structural · INFERRED (traced)
`atlas/dcp/backtest/portfolio.py` raises on malformed panels (`empty panel`, `panel dates must be
strictly ascending`, symbol/length mismatches — `:58`–`:66`); the engine contract is that a strategy
sees only `bars[:i+1]`. This is a structural guard, consistent with the invariant, but the guarantee
rests on the engine contract rather than a dedicated executed look-ahead test in this pass. See
`POINT_IN_TIME_AND_BIAS_ANALYSIS.md`.

### EV-15 — Reconciliation break ⇒ kill · INFERRED (traced)
`atlas/ops/daily.py`: "A chain break or reconciliation break is a KILL" (`:102`); paper reconciliation
writes `trading.reconciliations` and treats a break as terminal (`:221`–`:242`). Traced by inspection;
the kill-on-break path was not exercised in this pass.

## C. Evidence that is NOT available / NOT executed (honest gaps)

### EV-10 — `make replay` · NOT EXECUTED (UNSAFE SIDE EFFECT)
`atlas/dcp/market_data/replay.py` uses `session_scope()` (`:13`, `:27`) → writes the **development**
database and audit chain. Out of scope for a non-mutating evidence pass. The CLAUDE.md claim
"`make replay DATE=2024-07-15` → gate=green" is therefore **CLAIMED**, not verified here.

### EV-11 — Historical backtest performance (+737.31% vs SPY TR +593.89%, p=0.000, DSR 0.995, WF 4/4) · UNKNOWN / NOT REPRODUCED
These figures appear in CLAUDE.md/ADR-0010/package docs. This pass **did not** reproduce them: the
full gauntlet needs the licensed EODHD snapshot and excessive runtime, and — decisively — the
historical run is **not connected to a single (config + commit + data-snapshot + seed) artifact** that
could be re-executed to the same numbers (see `BACKTEST_REPRODUCIBILITY.md`). Reproducing today would
*generate a new result*, not reproduce the cited one. Classification: **CLAIMED** figures,
**reproducibility UNKNOWN** from this snapshot. Note EV-06 *does* reproduce the DSR arithmetic from a
raw Sharpe input, but not the Sharpe input itself.

### EV-12 — Live-model / desk LLM behaviour · NOT EXECUTED (MISSING CREDENTIALS)
No Anthropic API key; `anthropic` SDK absent (EV-02). All desk/agent runtime behaviour is
**NOT TESTED** in this pass; live-model evals remain pending per CLAUDE.md P2 status.

### EV-16 — Live broker / live order path · NOT FOUND / PLANNED
No live broker integration exists; Phase 7 is explicitly unbuilt (`01`, `16`, CLAUDE.md). Searching
`atlas/dcp/execution/` finds only `PaperBroker`. Classification: **PLANNED (NOT BUILT)**.

### EV-17 — API authentication/authorization · NOT FOUND
Per `14_SECURITY.md` and `16`, the FastAPI control surface has **no authentication**; safety rests on
an *assumed, code-unenforced* localhost-single-user posture. Treated as **NOT FOUND** (absent control),
a real residual risk carried into `EXECUTION_SAFETY_REVIEW.md` and `REMEDIATION_BACKLOG.md`.

### EV-18 — Point-in-time fundamentals · NOT FOUND / PLANNED
No PIT fundamentals vendor is wired; value/quality factors are explicitly unbuildable at this commit
(`01`, `04`, `17` Q4). **PLANNED**, Principal vendor decision.

## D. How downstream documents must use this base

- A claim may be marked **VERIFIED** in a downstream doc **only** if it maps to an `EV-##` in section A
  (executed or exact-line) — or the doc executes/inspects its own evidence and records it here-style.
- Performance numbers (EV-11) are **CLAIMED / reproducibility-UNKNOWN** and must never be presented as
  reproduced.
- "Code exists" facts (a class, a config key, a docstring) are **CLAIMED** or **NOT TESTED** unless a
  command or exact-line inspection raised them to **VERIFIED**.
- No downstream document may introduce a performance figure not already present in the repository, and
  none may soften a **NOT EXECUTED / NOT FOUND / UNKNOWN** into an implied success.
