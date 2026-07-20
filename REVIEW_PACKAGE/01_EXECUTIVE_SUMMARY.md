# 01 — Executive Summary

> Written for the review committee. It states what Atlas is, honestly, and points to the
> documents that substantiate (or challenge) each claim. Read `16_KNOWN_LIMITATIONS.md` and
> `17_OPEN_QUESTIONS.md` alongside this — they are the counterweight to any positive framing here.

## Purpose of the platform
Atlas AI Capital is an **AI hedge-fund operating system** running a **hypothetical A$100,000,
US + India equities, long-only, PAPER-MODE-ONLY** book. It is a *research + simulation* system,
not a trading system: there is **no broker, no real capital, and no live order path** — a
`PaperBroker` simulates next-session-open fills. Its reason for existing is to test whether an
**AI research desk governed by hard rules** can generate and validate investable strategies
without the failure modes that usually sink quant + LLM systems (overfitting, look-ahead,
data-mined "edges", LLMs inventing numbers, and undisclosed risk). See `02_SYSTEM_ARCHITECTURE.md`.

## Investment philosophy
Four commitments, enforced in code, not prose:
1. **Capital preservation first.** Absolute benchmark: a strategy must beat SPY *total return*
   after costs to be approved (ADR-0009) — outperforming a falling market is not enough.
2. **Nothing trades without surviving a gauntlet.** Null-model (1000-path monkey), deflated
   Sharpe at a lineage-scoped trial count, purged walk-forward, on a point-in-time,
   delisting-inclusive S&P 500 panel. Gates are never weakened to pass a strategy — 8 of 9
   tested lineages are in the graveyard. See `04_FACTOR_LIBRARY.md`, `08_BACKTESTING.md`.
3. **The machine proposes; the human disposes.** Every trade requires human approval; no LLM
   output ever becomes a sizing/pricing number (the "no-agent-numbers" wall). See `09_AI_AGENT_DESIGN.md`.
4. **Honest failures are deliverables.** Verdicts are recorded verbatim in an append-only audit
   chain; a failed gate is the system working. See `07`, `08`, `16`.

## Current maturity — *early, single-machine, one-strategy*
| Dimension | Honest state |
|---|---|
| Age | ~10 days of git history (137 commits, 2026-07-11→20); built by one Principal + an AI pair-builder. |
| Trading | **Paper only.** Live trading (Phase 7) is designed but **entirely unbuilt** — no broker integration. |
| Validated strategies | **Exactly one** (`xsmom-pit-tr`, 12-1 momentum, state 'paper'). Everything else is graveyard, suspended, or experimental. |
| Book state | **Was 100% cash** at review time (core proposals expired unapproved); first fills (AMD, INTC) settle in the next cycle. |
| Deployment | **One machine** (the Principal's MacBook), one process, Docker Postgres. **Zero backups taken to date** (first ever scheduled the night of the review). |
| Data | **One vendor** (EODHD, $99/mo). No point-in-time fundamentals → value/quality unbuildable. |
| Testing | ~1,515 tests, CI on push; but coverage measured only on the risk engine. |

This is a **credible research prototype with unusually strong process discipline**, not a
production fund. Do not evaluate it as the latter.

## Current capabilities (what is genuinely [IMPLEMENTED])
- A **point-in-time backtest gauntlet** (delisting-aware engine, monthly rebalance, next-open
  fills, flat 10 bps/side costs, null-model + deflated-Sharpe + purged walk-forward). `08`.
- A **six-role LLM research desk** (scanner → specialists → bull/bear debate → CIO memo) with a
  **grounding cage** (every quoted number must appear in cited evidence) and a **budget breaker**. `09`.
- A **deterministic memo→proposal bridge** (ADR-0006): sizes, derives stops, risk-checks — the
  only path from reasoning to a trade. `05`, `06`.
- An **11-limit risk engine** (L1–L11) + latched drawdown breakers + a property-tested vol-target,
  with **100% branch coverage** on the engine. `07`.
- An **append-only audit hash-chain** with nightly tamper verification. `14`.
- A **daily T0–T9 operating cycle** (one atomic checkpointed transaction/day) run by an in-process
  scheduler. `02`, `15`.
- A **Research Factory** (console-driven recipe gauntlet, feature store, pre-registration +
  advisory-locked one-experiment-one-name discipline). `04`, `08`.
- **Measured-never-applied research surfaces**: per-name dossier, opportunity screen,
  investing.com edge trial — none reach capital. `05`.
- A **single-file web console** as the sole control surface; a read/trigger **FastAPI**. `02`, `14`.

## Major limitations (the five that matter most — full list in `16`)
1. **One validated strategy carries the entire (40%-of-NAV) invested book** — extreme
   concentration, now by design (ADR-0017). No fallback sleeve: demotion → 100% cash.
2. **No point-in-time fundamentals** → value/quality factors cannot be built honestly and are not.
3. **Operational fragility**: single machine, single process, no backups until the review night,
   no independent restore drill, macOS sleep/TCC/iCloud hazards, no monitoring.
4. **No API authentication/authorization** — safe only under an *assumed* localhost-single-user
   posture that no code enforces.
5. **Paper-only realism gaps**: fills at next-open at the open price; flat 10 bps costs; no
   slippage/impact/spread; daily-granularity stops. Real-world costs and fills untested.

## Major risks (ranked)
1. **Strategy risk** — the momentum edge may not survive out-of-sample or a regime change; the
   +737% headline is a concentrated backtest, not expected return (its own drawdown band is −40%).
2. **Operational risk** — a laptop that sleeps, a process that dies, or a restore that has never
   been proven could halt or lose the fund's state.
3. **Data risk** — a single vendor; an internally-consistent bad datum would pass the coarse gates.
4. **Security risk** — an unauthenticated control surface; plaintext secrets.
5. **Research-immaturity risk** — most "signal" (learning loop, screens, edge trials) is measured,
   not yet predictive; the fund may simply have one strategy and a lot of honest scaffolding.

## Future roadmap (what is [PLANNED — NOT BUILT])
- **Sleeve #2 via PIT fundamentals**: adopt a point-in-time fundamentals vendor (Sharadar SF1
  recommended) → build + gauntlet value/quality families. *Blocked on a Principal vendor decision.*
  `docs/reports/pit-fundamentals-vendor-decision.md`.
- **Operational hardening**: migrate off the MacBook to a Linux host (scripts exist, deferred);
  prove the backup/restore drill.
- **Research Factory phase 3**: machine-authored hypotheses + an adoption pipeline into the
  Principal's approval queue.
- **Learning-loop Tier-1 activation** (~60 sessions of labels + a signature) — cross from
  measured to applied conviction weighting.
- **Live trading (Phase 7)**: broker integration, live risk loop, arming — a large, unbuilt phase.
- **Evidence arriving on a calendar** (not a build): first scorecard grades + source-pick edge
  (~early Aug 2026), regime robustness of the momentum sleeve over time.

## The one thing the reviewer should test hardest
Whether the **process discipline is real and load-bearing** (audit chain, no-agent-numbers wall,
refuse-to-weaken-gates, honest graveyard) — or whether it is rigorous scaffolding around a single,
possibly-fragile momentum bet. If the discipline is genuine, Atlas is a defensible foundation to
build a real fund on. If the one strategy fails out-of-sample, there is currently nothing behind it.
