# EXECUTION_SAFETY_REVIEW — the path from an AI thought to a (paper) order

> Anchored to `REPOSITORY_SNAPSHOT.md` (commit `2ba38c0`). Paper-mode only: the terminal actuator is
> `PaperBroker` (no live broker exists — EV-16). Classifications per `EVIDENCE_BASE.md`.

## 1. The full signal → order trace (who produces what)

```
[DCP] scanner v1 (deterministic, NOT an LLM)         atlas/dcp/scanner/v1.py
   → candidate names + evidence blocks
[LLM] research desk: bull/bear debate → specialists → CIO memo   atlas/agents/desk.py
   → a MEMO of reasoning + evidence references.  NO sizing/pricing/execution numbers.
       · grounding cage: every quoted number must appear in cited evidence  (atlas/agents/…verifier)
       · Pydantic schemas reject a BUY that lacks DCP evidence refs         (atlas/agents/schemas/)
[DCP] memo → proposal BRIDGE (ADR-0006)              atlas/dcp/trading/bridge.py
   → "derives every number from vendor bars alone" (:4); qty via risk engine's
     size_position through build_proposal (:17); it is the ONLY live caller of
     build_proposal, pinned by tests/unit/test_policy_conformance.py (:60-61);
     "the window fails closed — never reaches past an incomplete bar" (:74)
[DCP] risk engine L1–L11 + sizing                    atlas/dcp/risk/engine.py  (100% branch cov, EV-07)
   → a trade_proposal row (with evidence refs + real quant.signals ids)
[HUMAN] approval via the console/API                 atlas/api/routers/trading.py:145 approve_proposal
[DCP] FRESH risk re-check AT approval                atlas/dcp/trading/proposals.py:912 approve()
   → recheck_at_approval (:119 import); a fresh FAIL ⇒ RISK_RECHECK_FAILED, approval VOIDED, terminal
     (:881,:975).  Tested: tests/unit/test_approval_recheck.py — 5 passed (EV-09)
[DCP] order created → PaperBroker.submit             atlas/dcp/execution/paper.py:135,143
   → next-session-open fill; FIFO sell settlement; idempotent (proposals.py:1114)
[DCP] daily reconciliation                           atlas/ops/daily.py:221  (break = KILL, :102)
```

**The LLM appears at exactly one stage** (the memo) and emits **no number that reaches an order**.
Every numeric value on a proposal is produced by DCP (bridge + risk engine) from vendor bars. Two
deterministic gates sit between the AI and any fill: the **bridge** (fail-closed, only caller of
`build_proposal`) and the **fresh risk re-check at approval** (voids on FAIL). A **human** sits between
the proposal and the order.

## 2. The eight required questions

| # | Question | Answer | Evidence | Classification |
|---|---|---|---|---|
| 1 | Can any LLM **place, alter, cancel, or approve** orders? | **No.** Order creation happens only inside `proposals.approve()`, reachable only via the human `approve_proposal` API call; no agent code calls it. The no-agent-numbers wall means an LLM cannot even emit a size/price. | `trading.py:145`; `proposals.py:912`; EV-05 (red-team 9 pass); two-plane wall EV-03 | **VERIFIED** (tests executed) |
| 2 | Could an LLM place an order **indirectly**, via an unvalidated response feeding a numeric path? | **No.** The memo is schema-validated; a BUY without DCP evidence refs is a validation error; the grounding cage rejects quotes absent from evidence; the bridge re-derives every number from bars, ignoring any LLM number. | EV-05; `bridge.py:4,17`; `atlas/agents/schemas/` | **VERIFIED** (red-team) + **INFERRED** (bridge trace) |
| 3 | Is agent output **schema-validated** before use? | **Yes.** Pydantic schemas in `atlas/agents/schemas/`; the 9-test red-team proves malformed/number-injecting outputs are rejected. | EV-05 | **VERIFIED** |
| 4 | Are there **deterministic controls after the AI and before the broker**? | **Yes — two.** (a) the deterministic bridge (fail-closed, sole `build_proposal` caller, numbers from bars); (b) the fresh risk re-check at approval that VOIDS on FAIL. | `bridge.py:60-74`; EV-09 | **VERIFIED** (recheck test) + **INFERRED** (bridge) |
| 5 | Can **live trading be enabled accidentally**? | **No (three barriers).** `trading_mode` defaults to `"paper"` and `"live" additionally requires daily arming` (config comment); and **no live broker class exists** — Phase 7 is unbuilt. Flipping the flag alone actuates nothing. | `atlas/core/config.py:10`; EV-16 | **VERIFIED** (config line) + **NOT FOUND** (no live broker) |
| 6 | Can an order be **double-submitted**? | **Guarded.** Settlement is idempotent ("a filled order is never pending again", `proposals.py:1114`); the bridge refuses to bridge a memo twice in any state (`bridge.py:34`); approval outcomes are terminal. | `proposals.py:1114`; `bridge.py:34` | **INFERRED** (traced; not re-executed here) |
| 7 | Can the system **trade on stale data**? | **Mostly guarded, coarsely.** The bridge "fails closed — never reaches past an incomplete bar" (`bridge.py:74`); a coarse data-quality gate (missing-day RED / >40%-move AMBER) exists per package `03`. **Residual:** the quality gate is structural/coarse and single-vendor; a plausible-but-wrong datum can pass. | `bridge.py:74`; quality gate CLAIMED (`03`, Q12) | **INFERRED** + residual risk |
| 8 | Can the system **trade while reconciliation is failing**? | **No.** "A chain break or reconciliation break is a KILL" — the daily cycle halts and pages the operator; paper reconciliation treats any break as terminal. | `atlas/ops/daily.py:102,221-242`; EV-15 | **INFERRED** (traced; kill path not exercised this pass) |

## 3. Residual execution-safety risks (the honest part)

- **R-1 · No API authentication (High).** The human approval gate (`approve_proposal`) has **no
  auth/authorization** (EV-17, `14_SECURITY.md`). Safety rests on an *assumed, code-unenforced*
  localhost-single-user posture. Anyone able to reach the port can approve/cancel orders. In paper
  mode the blast radius is simulated state + the audit chain — but the control is genuinely absent.
- **R-2 · Daily-granularity stops (Medium, paper-acceptable / live-disqualifying).** Stops are
  pre-authorized and scanned at T4 daily (`exits.py`), not intraday (Q10). Fine for paper; a decision
  item before any live phase.
- **R-3 · Coarse stale-data defense (Medium).** As in Q7 — single vendor, structural quality gate; no
  cross-source reconciliation (Q11/Q12).
- **R-4 · Operating-loop fragility (Medium).** The in-process scheduler has documented missed/late
  fires (`01`,`16`); "pending" fills can mean "the loop that would produce them did not reliably run."
  A safety property (nothing fills without a cycle) that is also an availability risk.
- **R-5 · Kill/idempotency paths not exercised this pass (Low-Medium, evidence gap).** Q6/Q8 are
  traced by inspection, not re-executed here; they are covered by the package's paper-trading tests
  (CLAIMED) but not independently run in this non-mutating pass.

## 4. Verdict scope
The execution *architecture* is the system's strongest safety property: a genuine no-agent-numbers
wall (tested), a deterministic fail-closed bridge, a human gate, and a fresh deterministic risk
re-check that voids on failure — **for paper mode, the AI cannot move the book on its own.** The
material residual is **R-1 (no auth)** plus the live-phase items (R-2) and the coarse data/loop risks.
An investment/production-readiness verdict is out of scope — that is the independent reviewer's call.
