# 16 — Known Limitations (the brutally honest document)

> Purpose: give the reviewer a complete, unflinching inventory of what is weak, missing,
> assumed, experimental, or debt. Nothing here is spin. Where a limitation is mitigated,
> the mitigation is stated but not used to excuse the limitation. Severity is this author's
> engineering judgment (🔴 critical / 🟠 material / 🟡 minor), not a formal risk score.

## 0. The one-sentence honest summary
Atlas is a **well-instrumented, honestly-gated, single-machine paper-trading research system
with exactly one validated strategy, one data vendor, no authentication, no real fills, and
no independent operational track record** — its genuine strength is process discipline (audit
chain, no-agent-numbers wall, refuse-to-weaken-gates), not investable edge or production maturity.

---

## 1. Investment / research limitations

- 🔴 **One validated strategy carries the entire book.** After ADR-0017 the invested book is
  100% a single 40%-of-NAV momentum sleeve (`xsmom-pit-tr`, 12-1 top-5). Every other tested
  signal is in the graveyard or suspended. Single-strategy concentration is the dominant
  portfolio risk and it is now *by design*. Evidence: `atlas/dcp/trading/bridge.py`
  `SLEEVE_BUDGET_FRACTION`, `docs/adr/0017-satellite-heavy-reallocation.md`.
- 🔴 **The headline +737% is a concentrated top-5 momentum backtest and must never be read as
  expected return.** It travels with large drawdowns (the strategy's own demotion band is
  −40%). The defensible evidence is the *gate verdict* (null p=0.000, deflated Sharpe 0.995,
  walk-forward 4/4 vs SPY total return), not the return magnitude. `docs/adr/0010`.
- 🔴 **No point-in-time fundamentals → value and quality factor families cannot be built
  honestly, and are NOT built.** EODHD restates fundamentals in place (no filing dates) and
  has no fundamentals for pre-2018 delistings. This is the single biggest research blocker.
  A vendor decision (Sharadar SF1) is open and unfunded. `docs/reports/pit-fundamentals-vendor-decision.md`.
- 🟠 **Backtest history depth is shallow for a momentum claim.** The PIT S&P 500 panel and
  EODHD tier constrain history; ADR-0004 explicitly accepts ~1yr for some gates and warns the
  results are not decision-grade at that depth. The momentum approval used a deeper regenerated
  panel, but 2012→present is still short relative to institutional multi-decade expectations,
  and spans a largely bull regime — regime robustness is under-tested.
- 🟠 **The "measured, never applied" research surfaces have no demonstrated predictive edge
  yet.** The health score, opportunity screen, and investing.com source-pick edge trial are
  hypotheses; their first honest 20-session verdicts arrive ~2026-08. Today they are
  scaffolding, not signal. `atlas/dcp/research/health_score.py`, `opportunity_screen.py`.
- 🟡 **Estimate-revisions / PEAD forward experiments are accruing, not producing.** The
  estimate-snapshot archive (`market.estimate_snapshots`) needs ~6 months before it can test
  its factor; PEAD is suspended at 0% budget as a paper watch.

## 2. Data limitations

- 🔴 **Single data vendor (EODHD) — hard lock-in.** All prices, FX, dividends, splits,
  fundamentals, earnings flow through one $99/mo vendor via one adapter
  (`atlas/dcp/market_data/adapters/eodhd.py`). No redundancy, no cross-validation against a
  second source, no vendor-outage fallback beyond the keyless fixture adapter (which serves
  synthetic data, not production). A vendor data error would propagate silently past the quality
  gates if it is internally consistent.
- 🔴 **Dividend ingest is MANUAL-ONLY — the total-return benchmark silently decays between
  hand-runs.** Dividends are populated solely by a hand-run batch
  (`atlas/dcp/market_data/dividends.py`); neither the nightly cycle (T0 ingests bars + FX +
  splits only) nor the general backfill fetches them. Since ADR-0009's binding approval bar is
  *SPY total return*, and the TR series reads these dividend rows, dividends paid after the last
  manual run are absent until someone re-runs the batch — the benchmark the entire fund is judged
  against degrades over time with no automatic refresh and no alert. Surfaced by the internal
  adversarial audit of this package; a genuine operational + methodological gap.
- 🟠 **Corporate-action coverage is split-only for the return engine.** Splits and delisting
  liquidation are the hardened cases; spinoffs, mergers, symbol changes, and rights issues are
  not explicitly modelled — they rely on the vendor's own handling. Quarterly fundamentals
  (`quarterly_fundamentals`) are likewise manual-refresh, not on any automatic cadence.
- 🟠 **Quality gates catch structural gaps, not subtle bad data.** RED on missing days / AMBER
  on >40% unexplained moves (`atlas/dcp/market_data/quality.py`) is coarse; a plausible-but-wrong
  price inside the band passes. No statistical outlier detection, no cross-vendor reconciliation.
- 🟡 **India is ETF/ADR-only; direct NSE is impossible** (EODHD has zero NSE coverage). Post
  ADR-0017 India exposure is currently *nil*.

## 3. Portfolio & risk limitations

- 🔴 **No portfolio optimizer.** Construction is equal-weight top-5 within a sleeve budget
  (`bridge.py`). No mean-variance, no risk-parity, no covariance-aware weighting, no factor-
  neutralization at the book level. Diversification is enforced only by the L1/L3 caps.
- 🔴 **VaR and CVaR are NOT implemented.** There is no parametric or historical VaR, no expected
  shortfall, no tail-risk metric anywhere in `atlas/dcp/risk`. Risk is limit-based (L1–L11) +
  drawdown breakers + a gross-exposure cap, not distribution-based.
- 🟠 **The property-tested "≤0.80 gross" cap is on an UNWIRED scaler — the live gross ceiling is
  ~0.90.** `atlas/dcp/risk/vol_target.py`: `MAX_GROSS=0.80` is tested but belongs to the
  `target_gross_exposure` Tier-1 scaler that nothing calls; the *wired* `gross_step_gate`
  enforces 1 − L5 (10% cash floor) = **0.90**. So the book can deploy ~90% gross, not 80% —
  a doc-vs-live gap the internal audit caught; do not read the 0.80 property test as the live limit.
- 🟠 **No explicit beta or factor-exposure targeting.** Factor overlap (§12) checks pairwise
  concentration; it does not target or neutralize market/style beta.
- 🟠 **Stops are daily-granularity and pre-authorized, not live.** `T4` scans stops once per
  daily cycle (`atlas/dcp/trading/exits.py`); there is no intraday or real-time stop monitoring.
  A gap-through-stop is only caught at the next cycle. Fine for paper/next-open fills; unacceptable
  for live without redesign.
- 🟠 **Risk limits are calibrated to a hypothetical A$100k `small_aum` set, not empirically
  tuned.** The specific numbers (8% stock, 25% sector, 1% risk/trade, etc.) are policy choices
  in `risk.limit_sets`, defensible but not derived from a capacity or drawdown study.

## 4. Execution & "trading" limitations (it is paper)

- 🔴 **No real broker, no real fills, no real slippage.** `PaperBroker` fills at the **next
  session open at the open price** — optimistic vs real markets (no partial fills, no queue
  position, no market impact, no adverse selection). Reconciliation is paper-vs-paper.
- 🟠 **Flat 10 bps/side cost model (5 commission + 5 slippage).** No spread, no market impact,
  no borrow/short cost (moot — long only), no cost scaling with size or liquidity.
  `atlas/dcp/backtest/real_run.py`. Real transaction costs for a concentrated monthly rebalance
  could differ materially.
- 🟠 **Live trading (Phase 7) is entirely unbuilt.** `trading_mode='live'` and "arming" are
  designed and referenced but there is no broker integration, no order-routing, no live risk
  loop. Do not evaluate this as a trading system; it is a trading *simulator + research desk*.

## 5. Engineering & operations limitations 🔴 (the weakest domain)

- 🔴 **Single machine (the Principal's MacBook), single process.** Postgres in Docker, one
  uvicorn process that IS the scheduler. No HA, no failover, no redundancy. If the laptop
  sleeps, closes, or the process dies, the fund stops operating.
- 🔴 **The fund has taken ZERO backups to date.** The launchd backup + scheduler jobs have been
  dead since installation — macOS TCC denies launchd access to `~/Documents` (exit 127 every
  run). This was discovered 2026-07-20. The first backup ever is scheduled for 2026-07-21 via
  the in-process scheduler. There is **no verified restore drill on real data** (the
  `ops/migrate_from_dump.sh` restore path exists but has never been exercised against a real dump).
- 🟠 **`~/Documents` is iCloud-synced** → conflict-copy files (`" 2.py"`) appear under heavy
  parallel writes; the repo lives in hostile territory for a background service.
- 🟠 **A bare API restart silently disarms the scheduler AND drops the Anthropic key** unless
  `.env` is sourced (nothing loads `.env` into `os.environ`). This bit the operator twice on
  2026-07-20 (a 401 outage and a disarmed cycle). Guarded now by `.env` content, but the
  fragility is inherent to the single-process design.
- 🟠 **No monitoring/alerting stack.** Optional ntfy webhook (`ATLAS_ALERT_URL`) that is usually
  unset and degrades to stderr. No metrics, no dashboards of system health, no paging.
- 🟡 **A Linux-box migration is the recommended fix and is deferred by the Principal.**
  Provisioning + restore scripts exist (`ops/provision.sh`, `ops/migrate_from_dump.sh`) but the
  migration has not happened.

## 6. Security limitations 🔴

- 🔴 **The API has NO authentication and NO authorization.** Any process that can reach
  `localhost:8001` can drive the entire fund (approve trades, run cycles, trigger the desk).
  Step-up-token/scope plumbing is explicitly "deferred to the auth phase"
  (`atlas/api/routers/trading.py`). This is acceptable ONLY under the single-trusted-machine,
  localhost assumption — which is an assumption, not a control.
- 🟠 **Secrets are plaintext in `.env`.** Anthropic + EODHD API keys and DB credentials; the DB
  password is a literal default `atlas_local_only`. No secrets manager, no encryption at rest,
  no TLS on the API. Mitigations: `.env` is git-ignored; the repo is public but keys are not in it.
- 🟠 **No key rotation, no secret scanning, no `.env.example`.** Key rotation is a standing TODO.
- 🟡 **Supply chain: dependencies are floor-pinned (`>=`), not lock-filed.** No `requirements.lock`
  / hash pinning; `pip install -e ".[dev]"` resolves latest-compatible. No SBOM, no dependency
  audit in CI. (Positive: SQL is parameterized via SQLAlchemy `text()` — no raw f-string
  interpolation found; the audit hash-chain is genuinely tamper-evident.)
- Note on the audit chain: it is **tamper-evident, not tamper-proof** — an attacker with DB
  write + the ability to recompute hashes forward could rewrite it; it is not externally
  notarized or signed. Credit it as a strong internal integrity control, not a cryptographic
  guarantee against a privileged adversary.

## 7. AI / agent limitations

- 🟠 **Agents are effectively stateless per run.** There is no long-term agent memory or
  cross-run learning inside the agents; "memory" is the audit trail + the (measured-only)
  learning loop. Each committee run re-derives from evidence.
- 🟠 **The learning loop is MEASURED, NEVER APPLIED.** Brier-scored conviction weights are
  computed and displayed but modulate nothing; Tier-1 activation is an unbuilt future Principal
  signature. So the system does not yet "learn" in any behavior-changing sense.
- 🟠 **LLM dependence is a single-vendor (Anthropic) reliance** with an OpenAI-compatible escape
  hatch that is untested in production. Model behavior can drift between versions; the shadow-
  comparison harness exists but a switch is a manual decision.
- 🟡 **Grounding cage is token-level, not semantic.** It guarantees quoted numbers appear in
  evidence; it does not guarantee the *reasoning* is sound — a well-grounded but wrong thesis
  can pass. Mitigated by the no-agent-numbers wall (the LLM cannot size anything regardless).

## 8. Testing limitations

- 🟠 **Global test coverage is not measured or enforced.** 100% branch coverage exists ONLY on
  the risk engine (`make cov-risk`). The suite (~1,515 passing by pytest collection; ~1,454
  `def test_` functions) is broad but its true line/branch coverage over `atlas/` as a whole is
  unknown and unmeasured.
- 🟠 **No performance / load / stress / chaos test tier.** Nothing tests behavior under data
  volume, concurrency, failover, or vendor outage.
- 🟠 **mypy strict only on `atlas/core` + `atlas/dcp` + `atlas/fxlab`.** `atlas/api`, `atlas/ops`,
  and `atlas/agents` are not strict-typed.
- 🟡 **The suite is actively churning.** Multiple real bugs were found and fixed in the week of
  this review (order-dependent flakiness, an ambiguous SPY resolution, a committed test-DB leak,
  a retry-count desync, a test-DB column-budget exhaustion). This is healthy honesty but signals
  the code is young and not yet settled.

## 9. Scalability limitations

- 🟠 **Per-name query loops.** The opportunity screen enriches top-N names with per-name
  valuation/model queries (~14s for 506 names); the dossier does likewise. Fine at S&P-500
  scale on one box; would not scale to a large universe or many concurrent users.
- 🟠 **Single Postgres, single process, mostly single-threaded.** Agent calls are sequential;
  there is no job queue (Redis is declared in config/`doctor.py` but **not used by any
  application code** — a misleading dependency). Horizontal scaling is not designed for.

## 10. Governance / process limitations

- 🟡 **One Principal, one AI pair-builder.** No independent code review culture beyond the
  adversarial-review workflows the AI runs on itself; no separate risk officer, no segregation
  of duties. The "human approval" control is a single person who is also the system's owner.
- 🟡 **CI exists but is the only automation.** `.github/workflows/ci.yml` runs ruff + mypy +
  pytest on Postgres 16 on push — a genuine gate — but there is no CD, no staging, no release
  process, no deploy automation.

---

### How to read this document
Every 🔴 above is either *by design and disclosed* (single strategy, paper-only, no PIT
fundamentals) or *operational youth* (no backups yet, no auth, single machine). None are hidden
gate-weakening or dishonest-number problems — the system's core discipline (Section 0) is intact.
The reviewer's highest-value scrutiny is on: (1) whether the momentum edge survives out-of-sample
and regime change, (2) whether the operational fragility is acceptable even for paper, and
(3) whether the "measured-never-applied" research will ever cross into signal.
