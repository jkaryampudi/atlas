# 04 — Risk Management Policy

Atlas AI Capital · v1.0 · Owner: Human Principal · The Risk Engine implements this document verbatim.

---

## 1. Philosophy

Risk management is a **structural property**, not an opinion. Limits live in a versioned database table (`risk.limit_sets`), are evaluated by deterministic code, and bind everything downstream. The CRO *agent* explains risk; the Risk *Engine* enforces it. There is no path — agentic or human-in-the-moment — around a failed check except formal limit change control (Doc 02 §7.3).

## 2. The Risk Engine veto

`RiskEngine.validate(proposal, portfolio_snapshot, limit_set)` returns a `RiskCheck` record: PASS or FAIL with itemised results per rule. Structural enforcement:

1. `trade_proposals` can transition to `pending_approval` only with a referenced PASS check.
2. The check is **re-run at human-approval time** against a fresh portfolio snapshot and current prices; a now-FAIL voids the approval action.
3. Orders reference the check; the Execution service verifies the reference before submission.
4. Agents hold no write permissions on `risk.*` tables (Postgres role enforcement).

## 3. Hard limits (limit set v1)

| # | Rule | Institutional default | Small-AUM mode | Evaluation |
|---|---|---|---|---|
| L1 | Max single-stock weight at cost | 5% | 8% | at proposal + approval |
| L2 | Max single-ETF weight | 10% | 15% | same |
| L3 | Max sector exposure (GICS sector; India ADRs/ETFs mapped) | 25% | 25% | post-trade pro-forma |
| L4 | Max India-sleeve exposure | 30% | 30% | pro-forma, incl. ADR look-through |
| L5 | Min cash reserve | 20% | 20% | pro-forma |
| L6 | Max portfolio risk per trade (entry−stop × size) | 1% of NAV | 1% | sizing input, see §4 |
| L7 | Max aggregate open risk (Σ position risk to stops) | 6% of NAV | 6% | pro-forma |
| L8 | Max pairwise correlation concentration | no new position with >0.8 90-day corr to an existing position if combined weight >12% | same | pro-forma |
| L9 | Max new positions per day | 3 | 2 | daily counter |
| L10 | Position liquidity | position ≤ 5% of 20-day ADV (trivially satisfied at this AUM; kept for scale-invariance) | same | at proposal |
| L11 | Unhedged FX exposure | ≤ 85% of NAV in non-AUD | same | pro-forma |

Every limit evaluates against **worst-case pro-forma**: portfolio as-if all currently pending approved-but-unfilled orders execute.

## 4. Position sizing (deterministic)

```
risk_budget      = NAV × 1%                          (L6)
per_share_risk   = entry_price − stop_price          (stop from strategy spec, e.g. 2×ATR(14))
raw_size         = risk_budget / per_share_risk
size             = min(raw_size,
                       L1/L2 weight cap ÷ entry_price,
                       liquidity cap L10)
size             = round_down_to_lot(size)
reject if size × entry_price < A$2,000               (minimum economic position)
```

Properties: position size is an output of risk, never an input from conviction. High conviction buys *thesis quality*, not extra size, in v1. (A conviction-scaled sizing model may be proposed later — through strategy change control with backtest evidence.)

## 5. Drawdown circuit breakers

Measured on NAV in AUD, peak-to-trough, marked daily EOD.

| Level | Trigger | Automatic action |
|---|---|---|
| DD1 | −5% from high-water mark | New-position risk halved (L6 → 0.5%); CIO must publish a portfolio review memo |
| DD2 | −10% | No new positions; PM agent produces full-book thesis re-underwrite; human review required to resume |
| DD3 | −15% | **Full halt.** Exit-only mode; every holding gets an explicit human keep/exit decision; post-mortem mandatory before re-arming |

Breaker state changes are audit events and cannot be cleared by agents. Resumption from DD2/DD3 requires the dual-confirmation human action.

## 6. FX risk

Non-AUD exposure is inherent (US + India instruments). Policy v1: **unhedged but bounded** (L11) and *attributed* — monthly reporting splits local-currency return from FX effect so FX drag is visible, not hidden. Hedging (via AUD-hedged ETF share classes where available) is a Phase 6 consideration, not v1 complexity.

## 7. Stress testing

Run weekly and pro-forma on every proposal batch (marginal impact). Scenario math is DCP (factor shocks applied to holdings via betas/sector mappings); the Stress Testing Agent selects scenarios and writes the plain-English summary.

Scenario library v1:

| Scenario | Shock definition |
|---|---|
| Broad equity crash | US −20%, India −25%, correlations → 1 assumption |
| Rates shock | +150bp US 10Y; duration-sensitive sectors shocked via beta table |
| India-specific shock | Nifty −15%, INR −8% vs USD |
| Sector collapse | Largest portfolio sector −35% |
| AUD spike | AUD +10% vs USD and INR (translation loss) |
| Liquidity event | Spreads 5×, fills at −2% slippage assumption |

Reported: pro-forma NAV impact, distance-to-breaker (which DD level each scenario would trigger), and single-name worst contributors. **Policy rule:** a proposal whose marginal effect pushes the "broad equity crash" scenario loss beyond −25% NAV fails risk.

## 8. Operational risk controls

Daily reconciliation of internal positions vs broker records (Phase 5+); any break freezes new orders for the affected market. Data-quality gates (missing bars, split anomalies, stale fundamentals) block signal generation. LLM outage degrades gracefully — the safety path (stops monitoring, limits, reconciliation) is LLM-free by design (Doc 01 §9). Cost breaker: daily LLM spend cap; breach halts the reasoning plane.

## 9. Model risk

Strategies carry live-vs-backtest tracking: if realised hit-rate or drawdown deviates beyond pre-registered tolerance bands (recorded at approval), the strategy auto-demotes to `paper` and requires re-validation. The Quant Validation Agent's checklist (overfitting: parameter sensitivity sweeps, deflated Sharpe; survivorship: dataset bias-class must be `pit` or documented; look-ahead: signal timestamps strictly precede action bars; OOS: mandatory holdout never touched during development, plus walk-forward) is codified as required artifacts — a validation report without the artifacts cannot reach `approved`.

## 10. Limit change control

Human-only, dual confirmation ≥ 1h apart, effective next trading day, versioned as a new `limit_set` row (old rows immutable). The Compliance Agent's monthly pack lists every limit change with before/after and rationale.

---

## Amendments v1.2 (ADR-0002/0003)

**§11 Volatility targeting (Tier 1).** Gross exposure scales daily toward a 10–12% annualised realised-vol target, bounded by max gross 0.80 (the L5 cash floor), max daily step ±10 percentage points of NAV, and never overriding DD breaker states (breakers dominate).

**§12 Factor-overlap guard.** In addition to L8, the risk engine decomposes pro-forma holdings into market/sector/momentum loadings; a new position is rejected if it raises any single factor loading above its registered cap. Two momentum names in one sector are one bet — the engine must see that.

**§13 Live degradation (CUSUM).** Two-sided CUSUM on live strategy returns vs backtest expectation; a breach auto-demotes the strategy to `paper` (Tier 1) and opens a revalidation task (Tier 2). Static tolerance bands remain as the backstop.

**§14 Implementation shortfall.** Decision, approval, and fill prices recorded per trade; realised shortfall recalibrates the backtester's cost model (Tier 1) and is a standing line in monthly attribution.
