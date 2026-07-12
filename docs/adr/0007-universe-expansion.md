# ADR-0007 — Universe expansion: S&P 100 + liquid India set (~110 names)

Date: 2026-07-12 · Status: Accepted · Decider: Principal (Jay)

## Context
The seed universe (9 instruments) existed to build and harden the pipeline —
crash-test dummies for the machinery, not an investment stance. With the
platform live (T0–T9 cycle, gates, desk, bridge, approval) and the vendor
plan upgraded to full history + fundamentals, universe breadth became the
binding constraint on opportunity discovery: the desk reviewed 9 names
exhaustively; nothing hunted.

## Decision
The tradable universe becomes:

1. **The S&P 100 constituents** (101 tickers incl. dual-class), taken from
   the vendor's index components (`OEX.INDX`) as a **pinned snapshot at
   adoption date**, recorded in `seeds/universe.json`. The universe does NOT
   auto-track index changes: constituent drift is adopted deliberately by
   editing the manifest (a reviewed git change), never silently.
2. **The liquid India sleeve**: US-listed ETFs INDA, INDY, EPI and ADRs
   INFY, HDB, IBN, WIT, RDY, plus the existing ASX-listed NDIA — all
   carrying `economic_exposure: ["IN"]` so L4 caps total India exposure by
   look-through regardless of wrapper (ADR-0002 stands: never NSE/BSE
   directly).
3. The seed ETFs SPY and QQQ remain (benchmarks and Broad-ETF instruments).

Sector names are normalised to GICS buckets at manifest-generation time so
L3 sector caps aggregate coherently across old and new entries.

## Consequences
- Deep backfill (2010→present) runs for every new name; per-instrument
  inception rules (quality v1.2) keep pre-listing days honest.
- The nightly SCANNER (deterministic, in the compute plane) sweeps the full
  universe and routes only a small shortlist to the LLM desk — breadth
  scales ~12×, desk cost stays capped by shortlist size.
- With A$100k and L1's 8% cap the book still holds ~8–12 positions: width
  buys selection quality, not position count.
- More names scanned = more implicit trials: the trial-registry /
  deflated-Sharpe discipline (ADR-0002) is what keeps wide scanning from
  becoming self-deception, and scanner rules themselves are strategy
  surface that backtesting must eventually validate.

Signed: Jay, 2026-07-12 ("universe approved").
