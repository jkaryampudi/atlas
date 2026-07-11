# 03 — Investment Policy Document (IPD)

Atlas AI Capital · v1.0 · Owner: Human Principal · Reviewed quarterly

---

## 1. Mandate

Manage a hypothetical portfolio of **A$100,000** in US and Indian equities with the objective hierarchy:

1. **Capital preservation** — avoid permanent loss of capital; drawdown control is the first-class objective.
2. **Risk-adjusted returns** — outperform the blended benchmark on a Sharpe/Sortino basis over rolling 12-month windows.

Base currency: **AUD**. All P&L, limits, and reporting in AUD; FX effects reported separately in attribution.

## 2. Style and horizon

Long-only. Swing (days–weeks) and position (weeks–months) trades. No leverage, no shorting, no derivatives, no crypto, no FX trading (FX exposure arises only as a by-product of foreign holdings and is managed under Doc 04 §6). No intraday/HFT.

## 3. Strategy sleeves

| Sleeve | Description | Horizon | Capital allocation (initial) |
|---|---|---|---|
| S1 Momentum | Trend + relative strength + volume confirmation; DCP strategy family `momentum/*` | 2–12 weeks | up to 40% |
| S2 Quality growth | Revenue growth, margin expansion, balance-sheet strength; fundamental screen + committee thesis | 3–12 months | up to 40% |
| S3 Regime overlay | Bull/bear/high-vol classification gates the other sleeves (risk-on/off), never trades directly | n/a | overlay |
| Cash | Reserve + dry powder | n/a | ≥ 20% at all times |

Sleeve definitions are specs; concrete strategy versions must pass the Doc 02 §7.4 approval gate before generating live signals.

## 4. Investable universe

### 4.1 United States
Common stocks and ETFs listed on NYSE/Nasdaq meeting all of: market cap ≥ US$2B; 20-day average dollar volume ≥ US$20M; price ≥ US$5; listed ≥ 12 months; data-quality score green. Target screened universe ≈ 500–700 names.

### 4.2 India
**Phases 1–5 (default):** India exposure exclusively via liquid India-country and India-sector ETFs listed in the US or Australia (e.g. broad-market and sector India ETFs). Rationale: as an Australian tax resident, direct NSE/BSE market access requires NRI/PIS or equivalent arrangements with custodial and tax complexity that is out of scope until the platform has earned it.
**Phase 6+ (conditional):** direct NSE large-caps (Nifty 100 constituents, ADV threshold ₹50 crore) **only if** a compliant broker/account route is established. The broker adapter abstracts this; no other system change required.
**Available immediately regardless:** US-listed ADRs of Indian companies (INFY, HDB, IBN, WIT etc.) count as US-market instruments with India economic exposure and are tagged accordingly for sector-exposure math.

### 4.3 Exclusions
Leveraged/inverse ETFs; SPACs pre-deal; stocks under exchange surveillance/ASM-GSM lists (India); anything failing the data-quality gate; IPOs < 12 months.

## 5. Portfolio construction rules

| Parameter | Institutional default | Small-AUM mode (recommended at A$100k) |
|---|---|---|
| Max single-stock weight (at cost) | 5% | 8% |
| Max single-ETF weight | 10% | 15% |
| Max sector exposure | 25% | 25% |
| Max single-country-sleeve (India) | 30% | 30% |
| Min cash reserve | 20% | 20% |
| Target position count | 16–20 | 10–14 |
| Max new positions per day | 3 | 2 |

The mode in force is a board-approved parameter recorded in `risk.limit_sets`. Rationale for small-AUM mode: at A$100k, 5% positions (~A$5k) across ~16 names incur proportionally heavy brokerage/FX friction and dilute the research funnel; 8% caps single-name damage at a level consistent with the 1%-risk-per-trade rule while keeping positions economically meaningful. This is Challenge A3 from the README, resolved in favour of a documented, versioned parameter rather than a silent deviation.

## 6. Entry and exit discipline

Every position requires, before approval: a thesis with kill criteria (Doc 02 §4.2), a stop level and target from the DCP, and a maximum holding period. Exits are triggered by: stop hit (mandatory, non-negotiable), kill criterion observed (PM agent flags, human approves exit), target reached (PM agent recommends scale-out/hold), thesis time-expiry (auto-review), or portfolio-level risk-off signal from the regime overlay.

Stops are monitored by the DCP daily (EOD basis, this is not an intraday system); stop execution proposals are auto-generated and fast-tracked to the human approval queue.

## 7. Benchmarks and performance definition

Blended benchmark: **50% S&P 500 TR (AUD-unhedged) + 30% Nifty 50 TR (AUD-unhedged) + 20% AUD cash rate**, matching the structural allocation. Reported monthly: TWR vs benchmark, Sharpe, Sortino, max drawdown, hit rate, average win/loss, exposure utilisation, cost drag (brokerage + FX + agent/LLM costs — agent costs are real costs and appear in net performance).

## 8. Rebalancing and review cadence

Daily: PM agent holding reviews; risk dashboard. Weekly: CIO portfolio review memo; watchlist refresh. Monthly: attribution pack; limit-utilisation review. Quarterly: IPD review; strategy sleeve performance vs expectation; strategy retirement decisions (a sleeve underperforming its backtest expectation beyond tolerance for 2 consecutive quarters enters formal review).

## 9. Prohibited activities

No averaging down past the original risk budget of a position. No re-entry into a stopped-out name within 10 trading days without a new committee memo. No trades during a global halt or when the data-quality gate is red for that market. No overriding of expired proposals — expired means re-run the workflow with fresh data.

## 10. Tax and jurisdiction notes (informational, not advice)

Portfolio is hypothetical initially; if it goes live, the Principal (an Australian tax resident) should obtain professional advice on: CGT discount interactions with swing horizons, US W-8BEN withholding, India ETF distribution treatment, and record-keeping. The platform's audit log doubles as tax-lot documentation (`trading.tax_lots`).
