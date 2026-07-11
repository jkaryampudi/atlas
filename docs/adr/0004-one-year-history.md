# ADR-0004 — Phase 1 exit accepts one year of history (EODHD tier constraint)

Date: 2026-07-11 · Status: Accepted · Decider: Principal (Jay)

## Context
The Phase 1 exit criterion (Doc 08) requires "2 years of adjusted history for the seed
universe ingested with zero red gates on a clean day". The live EODHD subscription tier
caps EOD history at exactly one year back (verified 2026-07-11: first available bar is
2025-07-11 for every seed instrument; older requests are silently clamped). The backfill
over the available window (2025-07-11 → 2026-07-10) completed with **zero red gates**:
US 251 sessions × 8 instruments = 2,008 bars, AU 254 sessions × 1 instrument = 254 bars,
313 FX rows, complete to the day.

## Decision
Phase 1 exit accepts **one year** of adjusted history in place of two.

## Rationale
One year of complete, gate-clean, split-adjusted real data is sufficient for Phase 1's
purpose: proving pipeline integrity (ingestion, corporate actions, calendars, FX, quality
gates, audit) on real vendor data. Phase 1 validates the plumbing, not any strategy.

## Conditions
1. **Phase 3 real-data strategy validation requires the full-history upgrade** before any
   approval decision is considered decision-grade. A strategy may be *run* against the
   one-year window, but its validation verdicts are not a basis for promotion until
   re-run on full history.
2. **Any backtest report produced on the one-year window must carry a small-sample
   warning prominently** (top of report, not a footnote): walk-forward folds and
   deflated-Sharpe estimates on ~250 sessions are indicative only.

## Consequences
README Phase 1 checklist amended; Doc 08 Phase 1 exit criterion carries this amendment;
the 2-year backfill re-run (`python -m atlas.dcp.market_data.backfill --years 2 --end …`)
remains the standing follow-up once the EODHD plan is upgraded.
