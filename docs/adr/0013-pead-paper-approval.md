# ADR-0013 — Second strategy approval: PEAD/SUE to PAPER (with caveats in ink)

Date: 2026-07-15 · Status: **Accepted** (signed by the Principal 2026-07-15) · Decider: Principal (Jay)

## Context
`pead-sue-tr` — earnings-surprise / post-announcement drift, Foster-Olsen-Shevlin
Standardized Unexpected Earnings, top-decile equal-weight monthly — was built as
the orthogonal second factor an external review asked for (ADR-0011 sequencing),
run through the IDENTICAL gauntlet momentum passed.

Its history is itself a validation of the process:
- The first gauntlet run returned FAIL (+588.05% vs SPY TR +591.02%).
- An adversarial audit found a **double-split-adjustment bug** (EODHD stores EPS
  already backward-split-adjusted; the code re-adjusted on read), which was
  SUPPRESSING the strategy. Fixed (commit `eca1e71`), re-run, re-audited.
- Corrected, the full-window gate **PASSES**: +616.75% vs SPY TR +591.02%,
  null p=0.000, deflated Sharpe 0.997 at the true trial count, walk-forward 4/4.
  The fix was verified correct across 16+ splits with no new look-ahead.

## Decision
The Principal, presented with the full record and my recommendation *against*,
elected to **approve PEAD/SUE for PAPER trading as the fund's second strategy.**
Executed via `python -m atlas.tools.approve_pead_paper`, which regenerates the
gate + walk-forward artifacts in-process and refuses on any missing/failing
artifact. State machine: backtested → validated → paper. Live trading remains
Phase 7, separately gated.

## Caveats accepted in ink (recorded, NOT waived — the approval is informed)
These are material and were the basis of my recommendation against approval:
1. **The pre-committed 2016 kill-only trial FAILS**: +362.92% vs SPY TR +363.05%
   (misses by 0.13pp). By the demote-only kill protocol this is a strike momentum
   did not incur (momentum passed both its full window and its kill test).
2. **Endpoint concentration is severe**: PEAD beats SPY TR at only **4 of 25**
   monthly-rolled endpoints, all in the final ~4 months (Apr–Jul 2026); at
   2026-03-31 it was still trailing. The edge is ~one quarter old.
3. **Not orthogonal to momentum**: the outperformance is driven by a single
   AI/semiconductor cluster (MU, AMD, AMAT, …) that OVERLAPS momentum's winners,
   so a momentum+PEAD sleeve is closer to *more concentrated momentum* than to
   diversification — the opposite of the exercise's stated goal.
4. Corrected from a real bug; two prior `pead-sue-tr` trials (corrupt + corrected)
   are on the permanent registry; deflated Sharpe uses the true count.

## Guardrails (provisional bands — tighten-only, per ADR-0010 pattern)
On `quant.strategies.tolerance_bands`, enforced by the daily band check:
- Sleeve drawdown worse than **−45%** from peak → auto-demote to `suspended`
  (backtest max DD was −41.17%; wider than momentum's given PEAD's higher
  volatility and the concentration risk above).
- Trailing 126-session sleeve excess vs SPY TR below **−20pp** → auto-demote
  (TIGHTER than momentum's −25pp: the edge is unproven out of its recent window,
  so it is held to a shorter leash).
- Demotion is machine-executed and latching; re-promotion is a Principal signature.

## Consequences
1. `quant.strategies` gains a second `paper` row; PEAD signals become
   constitutionally citable for BUY memos alongside momentum.
2. **Active satellite (ADR-0012) becomes momentum 15% / PEAD 15%** (equal-weight
   across the two paper strategies, within the 30% satellite envelope). Because
   their winners overlap, aggregate factor-overlap (risk L12) must be watched —
   the split may concentrate rather than diversify.
3. The scorecard grades PEAD vs SPY TR from approval; the −20pp band is the
   fast tripwire if the recent edge was a window artifact.
4. This approval is the strongest live test of ADR-0011's discipline: a factor
   that cleared the binding gate but failed the robustness signals was approved
   on the Principal's informed judgment, with every concern on the record.
