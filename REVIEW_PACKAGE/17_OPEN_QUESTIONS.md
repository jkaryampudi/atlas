# 17 — Open Questions (architectural & research decisions awaiting validation)

> Each item is a decision that has been *made* but not *validated*, or a fork that remains
> open. Format: **Q — the question · Current stance · What would resolve it · Owner.**
> "Owner: Principal" means it is a capital/vendor/policy call only the human can make.

## A. Research & alpha validity (the questions that decide if this is a fund)

1. **Does the 12-1 momentum edge survive true out-of-sample and regime change?**
   Stance: approved to paper on 2012→present (largely bull) with a pre-committed −40%/−25pp
   demotion band. Resolve: forward paper track record across a drawdown/regime shift + the band
   holding. Owner: time + the demotion machinery. *This is the single most important open question.*

2. **Is a concentrated top-5 the right implementable form, or is it overfit to concentration?**
   Stance: top-5 passed; the sleeve is 40% (8%/name). Resolve: sensitivity of the verdict to
   top-N (10/15) and to weighting scheme; a capacity/turnover-cost study at realistic size.

3. **Will the flat 10 bps/side cost model survive contact with real fills?**
   Stance: assumed. Resolve: a slippage/impact model calibrated to real (or paper-broker-with-
   spread) executions; re-run the gauntlet under it. A monthly concentrated rebalance is where
   costs bite.

4. **Do value/quality anomalies work on this panel — and is the PIT-fundamentals spend worth it?**
   Stance: unbuildable without a new vendor (Sharadar SF1 recommended). Resolve: the free
   delisted-coverage probe + a first gauntlet run once data exists. Given low-vol just FAILED,
   the base rate is uncertain. Owner: Principal (vendor spend).

5. **Do the "measured, never applied" surfaces (health score, opportunity screen, source-pick
   edge) have any predictive edge?** Stance: hypotheses. Resolve: the first 20/60-session
   graded verdicts (~Aug–Oct 2026) beating a dartboard. If not, they are honest scaffolding to retire.

6. **Is the null-model + deflated-Sharpe + walk-forward gauntlet strict enough — or too strict?**
   Stance: it has killed 8 of 9 tested lineages. Resolve: an overfit-canary confirms it rejects
   junk (it does); but is the DSR ≥ 0.9 bar and the 1000-path monkey null the right calibration
   for an absolute-return mandate? Worth an external quant's eyes.

## B. Portfolio & risk architecture

7. **Is a limit-based risk framework (no VaR/CVaR, no optimizer) defensible, or a gap to close?**
   Stance: L1–L11 + drawdown breakers + vol-target, equal-weight. Resolve: decide whether tail
   metrics and a covariance-aware constructor are required before live, or unnecessary for a
   5-name long-only book. Owner: Principal + risk review.

8. **Is 40% in one strategy an acceptable concentration even for paper?**
   Stance: signed (ADR-0017) with costs acknowledged. Resolve: the reviewer's judgment on whether
   the single-sleeve/no-fallback design (demotion → 100% cash) is prudent or reckless.

9. **Are the specific limit values empirically right?** Stance: policy choices for `small_aum`.
   Resolve: a drawdown/capacity study that derives (not asserts) 8% stock / 25% sector / 1% risk.

10. **Daily-granularity stops: acceptable for paper, disqualifying for live?**
    Stance: pre-authorized, scanned at T4. Resolve: an explicit decision on intraday/live stop
    monitoring before any live phase.

## C. Data engineering

11. **Single-vendor lock-in: acceptable risk, or does the fund need a second source?**
    Stance: EODHD only. Resolve: cost/benefit of a redundant price feed for cross-validation and
    outage tolerance. Owner: Principal.

12. **Is the quality gate (missing-day RED / >40%-move AMBER) sufficient bad-data defense?**
    Stance: coarse structural checks. Resolve: whether statistical outlier detection / cross-source
    reconciliation is needed given the fund trades on this data.

13. **Point-of-knowledge for future fundamentals: filing date vs 8-K release?** (If Sharadar
    adopted.) Stance: file-recommends keying on the Form-10 `datekey` (conservative, lags the 8-K).
    Resolve: whether factor timing needs the true disclosure moment. `docs/reports/pit-fundamentals-vendor-decision.md`.

## D. Operations, security, deployment

14. **Is single-machine, single-process operation acceptable even for paper — and for how long?**
    Stance: deferred Linux migration. Resolve: the reviewer's threshold for when the operational
    fragility (no backups until today, sleep/TCC/iCloud hazards) becomes unacceptable.

15. **When does the API need real authentication?** Stance: none; localhost single-principal.
    Resolve: a decision tied to the live phase and to any non-localhost exposure. Today's answer
    "never leaves localhost" is an assumption to make explicit and enforce (it is not enforced by code).

16. **Has the backup format actually been proven to restore?** Stance: restore script exists,
    never run on real data. Resolve: a restore drill (the script verifies the audit chain end-to-end
    — run it once against tonight's first real dump).

17. **Is the in-process scheduler (API == cron) the right operational model, or a fragility to
    replace with real cron/systemd?** Stance: current model after launchd/TCC failure. Resolve:
    the Linux migration decision.

## E. AI / agents

18. **Should the desk auto-re-opine on tracked picks, or stay click-gated?** Stance: manual
    (Principal chose cost-savings). Resolve: a cost/value call once the edge trial produces signal.

19. **When (if ever) does the learning loop cross from measured to applied (Tier-1)?**
    Stance: measured-only; needs ~60 sessions of labels + a signature. Resolve: the calibration
    data + the reviewer's view on whether outcome-conditioned conviction weighting is sound.

20. **Should the challenger model (sonnet-5) replace the incumbent?** Stance: shadow-compared
    (debate diversity 5/8 vs 1/8); no switch. Resolve: a Principal registry decision on the
    quality/cost trade-off. `docs/reports/shadow-model-comparison-2026-07-19.md`.

## F. Process / governance

21. **Is adversarial-review-by-the-same-AI a sufficient substitute for independent review?**
    Stance: the AI runs multi-lens adversarial workflows on its own changes (this package included).
    Resolve: the reviewer's judgment — this is precisely what GPT-5.6's independent pass is meant
    to test. *If this review finds material issues the internal reviews missed, the answer is "no".*

---

### The three questions the author would put first
1. **Does the momentum edge survive out-of-sample + a real drawdown?** (Q1) — everything else is
   secondary if the one strategy doesn't hold.
2. **Is the operational posture (no independent backups/restore, single machine, no auth)
   acceptable for the fund's current stage?** (Q14–16)
3. **Is the gauntlet's rejection of 8/9 lineages evidence of rigor, or of a bar so high nothing
   will ever pass — including things that should?** (Q6)
