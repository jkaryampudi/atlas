# 18 — Review Checklist for GPT-5.6

> You are simultaneously the **Investment Committee, Chief Quant Researcher, CTO, Principal
> Engineer, Chief Risk Officer, and Head of Data Engineering** of a top-tier quant fund,
> conducting an independent, adversarial review of Atlas. Your mandate is to CHALLENGE, not
> approve. Prefer evidence from the code. For each item: state your finding, cite the file,
> and rate **PASS / CONCERN / FAIL / CANNOT-VERIFY**. Escalate anything the internal docs
> (esp. `16_KNOWN_LIMITATIONS.md`, `17_OPEN_QUESTIONS.md`) failed to disclose.

## Meta-check (do this first)
- [ ] Do `16_KNOWN_LIMITATIONS` and `17_OPEN_QUESTIONS` actually disclose the weaknesses you
      independently find? List anything they omitted or soft-pedalled — that omission is itself a finding.
- [ ] Does any document overclaim relative to the code you read? Cite the discrepancy.
- [ ] Are "implemented" vs "planned/experimental/placeholder" tags accurate?

## As Chief Quant Researcher
- [ ] Is the 12-1 momentum approval statistically sound, or is the +737% an artifact of
      concentration/regime/period selection? Inspect `atlas/dcp/backtest/xsmom_pit_run.py`,
      `portfolio_validation.py`, `docs/adr/0010`.
- [ ] Is the **deflated Sharpe** correctly computed at the true trial count, and is the
      lineage-scoped counting (ADR-0016) a legitimate multiple-testing control or a loophole?
      `atlas/dcp/backtest/registry.py`, `docs/adr/0016`.
- [ ] Is the **1000-path monkey null** a valid null model (same universe, same construction),
      or does it under-state significance? `xsmom_pit_run.py` null path.
- [ ] Is the **purged + embargoed walk-forward** correctly purged (no leakage across folds)?
      `atlas/dcp/backtest/walkforward.py`.
- [ ] Is **look-ahead** truly structural (`bars[:i+1]`, PIT membership, split-cap-at-t), or can
      you find a leak? `atlas/dcp/backtest/portfolio.py` (`PanelView`), `atlas/dcp/features/`.
- [ ] Is **survivorship** actually handled (delisted names priced/liquidated), or cosmetic?
- [ ] Are the graveyard verdicts honest, and were gates ever weakened to pass anything? Check
      git history / `docs/reports/*.md`.
- [ ] Is the flat-cost assumption fatal to the edge? Estimate real costs for a monthly top-5 rebalance.
- [ ] Are the "measured-never-applied" factors (health score, opportunity screen) methodologically
      sound as *future* signals, or noise? `atlas/dcp/research/`.

## As Chief Risk Officer
- [ ] Is a limit-based framework with **no VaR/CVaR** acceptable for the mandate? What tail risk
      is unmeasured? `atlas/dcp/risk/engine.py`.
- [ ] Is **40% in one strategy with no fallback sleeve** (demotion → 100% cash) prudent?
      `docs/adr/0017`, `bridge.py`.
- [ ] Are the **L1–L11 limits** internally consistent and their values justified, or arbitrary?
      `risk.limit_sets`, `atlas/dcp/risk/seed_limits.py`.
- [ ] Do the **drawdown breakers (DD1–DD3)** actually latch and force de-risking? Trace the path
      from `t5b` band check → `suspended` state → book to cash.
- [ ] Are **daily-granularity stops** a material risk (gap-through between cycles)?
      `atlas/dcp/trading/exits.py`.
- [ ] Is **"risk FAIL is terminal"** truly unbypassable? Try to find a path around it. `bridge.py`,
      `risk/engine.py`, `tests/constitution/`.
- [ ] Is the **vol-target** invariant (≤0.80 gross) actually enforced? `atlas/dcp/risk/vol_target.py`.

## As Head of Data Engineering
- [ ] Is **single-vendor (EODHD) lock-in** an acceptable single point of failure? What is the
      blast radius of a vendor data error or outage? `atlas/dcp/market_data/adapters/eodhd.py`.
- [ ] Are the **quality gates** sufficient to catch bad-but-plausible data? `market_data/quality.py`.
- [ ] Is **corporate-action handling** complete (splits ✓; dividends ✓; spinoffs/mergers/symbol
      changes ✗)? `market_data/adjustment.py`.
- [ ] Is the **PIT-fundamentals gap** correctly characterized as blocking value/quality?
      `docs/reports/pit-fundamentals-vendor-decision.md`.
- [ ] Is **adjust-on-read** (raw bars stored, adjusted at read) correct and leak-free?
- [ ] Is the **feature store** PIT-honest (dataset_version pins, no restatement leakage)?
      `atlas/dcp/features/store.py`.

## As Principal Engineer / CTO
- [ ] Is the **two-plane wall** (dcp never imports agents; agents never touch risk/execution)
      actually enforced, or bypassable? `tests/unit/test_boundaries.py`.
- [ ] Is the **no-agent-numbers** invariant airtight — can any LLM output reach a sizing/pricing
      value? `atlas/agents/schemas/`, `bridge.py`.
- [ ] Is the **audit hash-chain** correct, and honestly scoped (tamper-evident ≠ tamper-proof)?
      `atlas/core/audit_repo.py`, `atlas/tools/verify_chain.py`.
- [ ] Is the **single-process, single-machine, no-backup-until-today** operational posture
      acceptable? Has the **restore** ever been proven? `ops/migrate_from_dump.sh` (never run).
- [ ] Is **console.html (2,264 lines, inline JS)** a maintainability liability?
- [ ] Is there **dead/misleading code** (Redis declared-but-unused; the Streamlit dashboard
      superseded by console.html but still present in `atlas/dashboard/`)?
- [ ] Is **injectable time** truly universal, or does `datetime.now()` leak into deterministic paths?
- [ ] Does **CI** (`.github/workflows/ci.yml`) actually gate merges (ruff+mypy+pytest on Postgres)?
- [ ] Is **test coverage** adequate given it's measured only on the risk engine?

## As Chief Information Security Officer (implied by the CTO hat)
- [ ] The API has **no authentication/authorization** — is the localhost-single-principal
      assumption enforced by anything, or just assumed? `atlas/api/` (no auth middleware).
- [ ] Are **secrets** (plaintext `.env`, literal DB password, vendor keys) handled acceptably?
- [ ] Is the **supply chain** controlled (floor-pinned deps, no lockfile, no SBOM)? `pyproject.toml`.
- [ ] Is SQL **injection-safe** (parameterized `text()` throughout)? Grep for f-string SQL.

## As Investment Committee (the synthesis)
- [ ] Would you allocate *real* capital to this system today? If not, what are the exact,
      ordered gates to yes?
- [ ] Is the **process discipline** (honest gates, audit trail, refuse-to-weaken) genuine and
      differentiating, or window-dressing over a one-strategy bet?
- [ ] What is the **single most likely way this system loses money** in live trading, and does
      any control address it?
- [ ] Rank the fund's risks: strategy concentration vs operational fragility vs data single-point
      vs security vs research immaturity. Where should the next effort go?
- [ ] Is the roadmap (sleeve #2 via PIT fundamentals; Linux migration; Tier-1 learning) the right
      priority order, or is something more urgent being deferred?

## Deliverable requested of the reviewer
A ranked list of findings (FAIL first), each with: file evidence, severity, whether the internal
docs disclosed it, and the specific remediation. Plus a one-paragraph verdict on **investability
today**, **investability path**, and **the biggest thing the builders are wrong about**.
