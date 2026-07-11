# 08 — Development Roadmap

Atlas AI Capital · v1.0 · Seven phases · Every phase has explicit entry/exit criteria; no phase begins until the prior phase's gate is signed.

Estimated effort assumes one senior engineer part-time with AI-assisted development. Durations are working-cadence estimates, not promises.

---

## Phase 0 — Package approval (now)

Deliverable: this architecture package. Exit: sign-off table in README completed; any challenged-assumption resolutions (A1–A7) explicitly accepted or amended; small-AUM mode decision recorded.

## Phase 1 — Foundation (≈ 3–4 weeks)

**Build:** repo skeleton + CI gates (Doc 07), Postgres schemas + migrations (Doc 05), `core` (config, clock, audit hash chain, outbox), market-data ingestion for US EOD + India-ETF proxies, corporate-action adjustment, data-quality gates, FX rates, portfolio snapshot math, read-only API (`/market`, `/portfolio`, `/system/health`), dashboard Overview page, seeded fixture dataset + deterministic replay harness.

**Explicitly not built:** any agent, any signal, any order path.

**Exit criteria:** 2 years of adjusted history for the seed universe ingested with zero red gates on a clean day *(amended to 1 year per ADR-0004 — EODHD tier constraint; Phase 3 decision-grade validation still requires the full-history upgrade)*; golden ingestion regression tests green; audit hash chain verifying nightly; `make daily-cycle` replays deterministically; NAV math matches a hand-computed fixture portfolio to the cent.

## Phase 2 — Research agents (≈ 3 weeks)

**Build:** agent runtime (budget guard, schema validation, untrusted-content wrapping, run logging), Constitution embedding, prompt templates + hashing, Market Scanner / Research Analyst / Macro / Sector Specialist / CIO roles, memo storage + dashboard Research page, constitution red-team test suite v1.

**Exit criteria:** full T1–T5 funnel runs on fixture data producing valid committee memos; every memo claim carries evidence refs (schema-enforced); red-team suite green (injection corpus, numeric-field escape attempts); daily cost within budget on a realistic day; INSUFFICIENT_EVIDENCE path demonstrated and respected.

**Gate question:** are the memos actually good? Human reviews 10 memos against own judgement; if the memos are confident nonsense, we stop and fix before building anything that could act on them.

## Phase 3 — Quant engine and backtesting (≈ 4–5 weeks)

**Build:** indicator library (property-tested), momentum v1 + regime classifier v1 strategy implementations, event-driven backtester with cost model, **PIT fundamentals ingestion (paid data dependency — budget decision here)**, quality-growth screen v1, Quant Research + Quant Validation agents, strategy lifecycle state machine, validation checklist artifacts, backtest regression suite.

**Known constraint:** India PIT fundamentals are poor; quality-growth sleeve launches US-only, and this limitation is recorded in the strategy spec, not discovered later.

**Exit criteria:** momentum v1 passes the full validation checklist including untouched OOS holdout and deflated-Sharpe reporting; a deliberately overfit "canary" strategy is **rejected** by the validation pipeline (the gate must be shown to catch bad strategies, not just pass good ones); regime classifier states match hand-labelled history ≥ agreed threshold.

## Phase 4 — Risk engine (≈ 3 weeks)

**Build:** RiskEngine with limit set v1 (all L-rules), position sizing, pro-forma portfolio math, correlation checks, stress-scenario math + Stress Agent + CRO agent narratives, drawdown breaker state machine, halts, limit change control with dual confirmation, dashboard Risk page, approval-time re-check plumbing.

**Exit criteria:** 100% branch coverage on `dcp/risk`; property tests prove no input combination produces a size violating any cap; every breaker transition tested; a simulated agent attempt to write limits fails at the DB role level (test exists and passes); stress marginal-impact math validated against hand calculations.

## Phase 5 — Paper trading (≈ 8–12 weeks calendar, mostly elapsed time)

**Build:** paper broker adapter, full T0–T9 live-daily pipeline on real data, approval queue UX with step-up auth, stop-monitoring + auto-generated exit proposals, reconciliation (vs paper broker), PM agent daily reviews, Attribution + Compliance agents, monthly packs.

**Operate:** minimum **60 trading days** of daily paper operation with real approvals treated seriously.

**Exit criteria (go/no-go for Phase 6):** ≥ 60 sessions with zero risk-engine bypasses and zero unexplained audit gaps; live-vs-backtest tracking within pre-registered tolerance bands (or strategies demoted per Doc 04 §9 — demotions working is itself a pass); reconciliation clean ≥ 98% of days with breaks resolved same-day; human approval workflow sustainable (if the approval queue is being rubber-stamped, that is a *fail* — fix the funnel volume); full cost accounting shows all-in cost drag (data + LLM + notional brokerage) at a level the strategy's edge plausibly clears. **This last criterion can kill the project economically, and it is better to learn that on paper.**

## Phase 6 — Broker integration (≈ 3–4 weeks)

**Build:** IBKR (or chosen broker) adapter behind the existing interface, real reconciliation, live market-data cross-checks, India access decision executed (direct NSE route if secured, else ETFs remain), FX handling for funding flows, `arm-live` mechanism, incident runbook.

**Exit criteria:** 10 sessions of shadow mode (live data, real broker connection, orders built but routed to paper) with zero divergences between shadow and paper decisions; failover drills passed (broker down, data stale, LLM down); runbook rehearsed.

## Phase 7 — Live trading with strict controls (ongoing)

**Ramp:** start at **25% of NAV deployed cap** for the first 20 sessions, then 50%, then policy-full (80%), each step gated on a clean review. Daily arming required; weekly human review non-negotiable; monthly compliance pack; quarterly IPD review.

**Standing kill conditions:** DD3 breaker, reconciliation break unresolved > 1 day, audit-chain verification failure, or any risk-engine bypass discovered → immediate halt and post-mortem before re-arm.

---

## Cross-phase workstreams

| Workstream | Cadence |
|---|---|
| Constitution red-team suite | nightly + every prompt change |
| Data-quality review | weekly |
| Cost tracking vs budget | daily, breaker-enforced |
| ADRs for every architectural deviation | as they happen |
| Model/prompt upgrade shadow runs | per Doc 02 §7.2 |

## Honest risk register for the project itself

| Risk | Mitigation |
|---|---|
| Edge doesn't clear costs at A$100k | Phase 5 exit criterion is an explicit economic go/no-go |
| India data quality undermines that sleeve | ETF-proxy default; direct names conditional |
| Agent quality plateaus at "plausible but shallow" | Phase 2 human memo-review gate; memos scored over time vs outcomes |
| Scope creep toward autonomy | Constitution + structural controls; any autonomy expansion is a Constitution amendment, i.e. deliberately heavy |
| Single-operator key-person risk | Runbooks, deterministic replay, everything reconstructible from the audit log |

---

## Amendments v1.2 — exit-criteria additions (ADR-0002/0003)

**Phase 3 adds:** trial registry live from the first backtest; the overfit canary must fail BOTH the validation checklist AND the null-model gate (buy-and-hold + 1,000-path random-entry MC); purged/embargoed walk-forward is the default CV; live/backtest single-code-path parity test green.

**Phase 4 adds:** vol-target scaler with bound tests (never exceeds 0.80 gross, never overrides breakers); factor-overlap guard tested; CUSUM demotion path tested.

**Phase 5 adds:** implementation shortfall recorded from session 1 and feeding the cost model; counterfactual ledger tracking all rejected/expired/stopped items; ONE full learning cycle demonstrated (miscalibration detected → Tier 1 adjustment → measured improvement) before Phase 6 go.
