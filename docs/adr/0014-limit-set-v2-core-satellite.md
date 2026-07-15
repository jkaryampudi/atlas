# ADR-0014 — Limit-set v2: risk limits for the core-satellite model

Date: 2026-07-15 · Status: **Accepted** (signed by the Principal 2026-07-16; option **B** chosen) · Decider: Principal (Jay)

## Context
`limit_set_v1` (`small_aum`, ADR-0001) was designed for a pure single-name
active book: lots of cash, tight single-name and ETF concentration caps, every
position stop-protected. ADR-0012 introduced a fundamentally different structure
— a **70% passive index core + 30% active satellite** — and four v1 limits now
block or mis-measure it. None of these are risk the fund actually wants to take;
they are a *classification* mismatch: v1 treats a broad index ETF as if it were
a concentrated single-name bet, and assumes every holding carries stop-out risk.

**Raising a cap for a broad index ETF is not weakening risk discipline.** A 55%
SPY position is the *least* concentrated equity holding possible — it is the
market, the very benchmark every gate measures against. The capital-preservation
mandate is served, not harmed, by owning it.

## The four blocking/mismeasuring limits (measured against the ADR-0012 targets)
| Limit | v1 value | Problem | v2 |
|---|---|---|---|
| `L2_max_etf_weight` | 0.15 | Blocks the 55% SPY / 15% INDA core; treats a broad index ETF as single-name concentration | Split the ETF class: **core-index ETFs** (an explicit allowlist: SPY, INDA) capped at **0.60** each; **all other ETFs** stay at **0.15** |
| `L5_min_cash_reserve` | 0.20 | The 20% floor was for a cash-heavy single-name book | **0.10** (option B, signed): a deliberate 10% cash reserve under the 70/20/10 allocation |
| `L9_max_new_positions_per_day` | 2 | A core rebalance (2 legs) + a satellite build (up to ~10) exceeds it; the cap was meant to throttle *discretionary* single-name entries | A **batched core/satellite rebalance counts as ONE authorized action** (origin `core_allocation` or a signed strategy rebalance); discretionary agent entries stay capped at **2/day** |
| `L7_max_aggregate_open_risk` | 0.06 | Core positions have `stop = entry` (rebalanced, not stopped); v1 fail-closes and counts their FULL value as open risk, which would blow L7 for satellite proposals | Open risk is **stop-based**: a position with no stop-out distance (core) contributes **zero** to L7; the 0.06 cap applies to the **stopped satellite** book only |

Unchanged and still binding: L1 (single stock 0.08 — applies to satellite names,
never to the core ETFs), L3 (sector 0.25), L4 (India sleeve 0.30), L6 (risk/trade
0.01), L8 (correlation), L10 (ADV), L11 (non-AUD 0.85), and the DD1–DD3 breakers.
The satellite's active book faces the identical discipline it does today.

## Decision
Adopt `limit_set_v2` (`small_aum`) with the four changes above, effective on
signature. The change flows through the existing dual-confirmation control
(`risk.limit_sets` requires `confirmation_a` + `confirmation_b` ≥ 1h apart);
v2 `supersedes` v1. The core-index ETF allowlist (SPY, INDA) is part of the
signed set — adding an instrument to it is itself a signed limit-set change, so
the 0.60 cap can never silently extend to an arbitrary ETF.

## Principal choice — RESOLVED: option B (signed 2026-07-16)
The Principal chose **(B)**: the allocation is amended to **70% core / 20%
active satellite / 10% cash**, and **L5 = 0.10**. ADR-0012 is amended
accordingly (core targets unchanged at SPY 55% / INDA 15%; the satellite
envelope drops from 30% to 20%, split momentum 10% / PEAD 10%). A deliberate
cash reserve consistent with the capital-preservation mandate; a smaller active
envelope while PEAD's edge is unproven.

(Option A — fully invested 70/30, L5 = 0.02 — was declined.)

## Consequences
1. The passive core can generate `core_allocation` proposals that PASS the risk
   engine and reach the approval queue — capital finally deploys (still
   human-sealed).
2. The satellite (momentum + PEAD) faces unchanged single-name discipline within
   its envelope; core exposure never counts against single-name or stop-risk caps.
3. The L7 stop-based redefinition is a real risk-engine change (a code change
   under 100%-branch coverage) — it must be built and adversarially reviewed
   before v2 is armed; the limit *values* are this ADR, the *engine change* is a
   reviewed implementation task gated behind it.
4. Every relaxation is scoped and named; nothing here loosens the discipline the
   satellite's active bets face.
