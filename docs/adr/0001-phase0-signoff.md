# ADR-0001 — Phase 0 sign-off and foundational decisions

Date: 2026-07-11 · Status: Accepted · Decider: Principal (Jay)

## Decisions

1. **Architecture package v1.0 approved** as drafted. Challenged assumptions A1–A7 accepted with resolutions as documented in the package README.
2. **Limit mode: `small_aum`** — 8% single-stock cap, 15% single-ETF cap, target 10–14 positions. Seeded as `risk.limit_sets` version 1. Reversible via limit change control (dual confirmation).
3. **India route (Phases 1–5): ETFs + US-listed ADRs only.** Direct NSE access deferred to Phase 6, conditional on a compliant broker/account route (NRI/PIS or equivalent). The `economic_exposure` field on instruments carries India look-through for L4.
4. **Primary EOD data vendor: EODHD** (US + global ETF coverage, corporate actions, FX). Chosen as default; the adapter interface (`atlas/dcp/market_data/adapters/base.py`) keeps this swappable. Fixture adapter is the development default — no vendor key required for local work.
5. **Trading mode default: paper.** Live arming mechanism deferred to Phase 6 per roadmap.

## Consequences

- Phase 1 build begins: repo skeleton, CI gates, Postgres schemas + migrations, core kernel (config, clock, audit hash chain), market-data ingestion with adjustment + quality gates, FX, portfolio NAV math, read-only API, fixtures + deterministic replay.
- No agents, no signals, no order path in Phase 1 (explicit non-goals).
