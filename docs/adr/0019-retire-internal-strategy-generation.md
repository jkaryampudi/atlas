# ADR-0019: Retire internal strategy generation — the investing.com S&P lane is the operating model

**Status**: PROPOSED — binding only once the Principal signs (merge = signature)
**Date**: 2026-10-18
**Amends**: ADR-0010 / ADR-0018 (xsmom stays `research_shadow`; no re-promotion
work is planned), ADR-0015 (PEAD sleeve stays at 0), ADR-0017 (the 40% sleeve
remains frozen shadow exposure; the book stays cash). Retires the "ADR-0019 book
shape" candidate referenced in `docs/specs/mom-12-1-top10.json` — moot while no
internal strategy exists.
**Does NOT touch**: any gate, threshold or approval criterion; strategy
mathematics; the risk limit set; the paper book; the audit chain; historical
results. Nothing here weakens a gate — it stops spending effort on strategies
the gates already rejected.

## Context

Every strategy Atlas generated internally has been measured and failed its bar:

- **AI desk** — graded below a direction-blind dartboard at 20 sessions (BUY
  −10.4pp, REJECT −11.7pp); off by pre-committed verdict since 2026-08-25. A
  2026-08-30 counterfactual found no veto that rescues it.
- **xsmom-pit-tr** — independent review REJECT (ADR-0018); lineage DSR ≈0.85
  below the 0.90 bar; re-promotion blocked on the identity feed.
- **PEAD** — implementable variant failed its gate (ADR-0015); monthly winner
  list generated at zero capital.
- **Opportunity screen** — negative edge over its record (one winning cohort).
- **Research Factory** — every recipe to date dead in ink.

The external source tracked through the same measurement machinery has not:
investing.com picks show **+12pp vs the dartboard at 60 sessions (n=25)** and
+16pp at 20 sessions (n=45). Split by point-in-time S&P membership (H3), the
edge is carried entirely by S&P picks (60s: 58% win, +10.8% mean, n=12) while
non-members lose (60s: 15% win, −11.3%, n=13). Samples are small.

## Decision

1. **Internal strategy generation is retired.** No further development effort,
   no capital path. The desk stays off (`ATLAS_DESK_ENABLED=0`); re-enabling it
   requires a new signed ADR with a reformed design and a fresh measurement
   window. The PEAD list and screen cohort keep running only because they are
   free measurements; neither may feed a proposal.
2. **The operating model is the investing.com pipeline**: ingest each list →
   pre-flight flags (knife, vol, hot, profile, liquidity, earnings) → grade at
   5/10/20/60 sessions vs SPY and the dartboard → registered hypotheses (H1–H3)
   → monthly cohort kill switch (two consecutive losing cohorts at 20 sessions
   stops the sleeve, no relitigation).
3. **Real-money gate** (positions live at the Principal's broker; Atlas
   measures, the paper book stays cash):
   - *Start* only after the first post-2026-10-18 cohort's S&P picks do not lose
     at 20 sessions.
   - *Lane*: S&P 500 members on the pick date only (H3). Non-members keep being
     tracked on paper.
   - *Size*: total at most ~10% of investable capital, equal-weighted across that
     month's S&P picks — the whole subset, never a hand-picked favourite.
   - *Hold*: 20 sessions; no fixed-percentage stops (simulated on the July
     cohort: hold +8.1% vs 5% stop +0.7% vs 10% stop +2.0%).
   - *Record*: actual fills logged and graded against the paper cohort.
   - *Stop*: two consecutive losing S&P cohorts at 20 sessions, or the 60-session
     S&P edge falling to ≤ 0.

## Consequences

- Effort shifts from building strategies to measuring one source well. Lists must
  keep arriving monthly for any of it to work.
- The edge claim is small-sample; it is re-measured every cohort and the stop
  conditions above end real-money use automatically if it decays.
- Infrastructure urgency drops: grading reads stored prices and catch-up after an
  outage is lossless (a 16-day gap backfilled 5,062 bars with nothing lost), so
  downtime delays verdicts without corrupting them.
- The data-vendor requirement shrinks to end-of-day prices, splits, dividends and
  FX for the picks and SPY; the fundamentals tier becomes optional (a separate
  Principal decision).
