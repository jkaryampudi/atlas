# Source-Pick Filter Hypotheses (pre-registered)

**Purpose.** Convert "why did the losing picks lose?" from post-hoc story-telling
into falsifiable, dated hypotheses. Each hypothesis is REGISTERED HERE **before**
the data that will judge it exists; the nightly grade accrual then evaluates it
out-of-sample. A hypothesis that survives may become a selection filter — but
only through the gated path below. **Nothing in this file changes selection
behaviour by itself.**

**House rules (binding):**
1. **Measured, never auto-applied.** A pick is scored, never eliminated by code,
   until a filter has (a) survived its out-of-sample test at the 20- and
   60-session horizons, and (b) been activated by a Principal-signed
   learning-loop Tier-1 decision. (Same bar as `dcp/research/autopsy.py`
   documents for fragility markers.)
2. **Out-of-sample means after registration.** Only picks whose
   `recommendation_date` is **strictly after** the hypothesis's registration
   date count toward its verdict. Picks from before are reported separately as
   in-sample context (they are the data that *suggested* the hypothesis and can
   never confirm it).
3. **Counterfactual tracking is mandatory.** If a filter is ever activated,
   filtered-out picks continue to be recorded and graded (tagged, not deleted),
   so the filter itself remains falsifiable and can be retired when it stops
   working.
4. **No gate is weakened; no strategy math is touched.** These hypotheses live
   entirely on the research/measurement surface (`research.source_picks`).

**Evaluation protocol (all hypotheses).** At each grading horizon
(5/10/20/60 sessions, vs SPY): split graded picks into the hypothesis's
IN-cohort (predicate true at pick time, from frozen `features`) and OUT-cohort
(predicate false). The hypothesis *survives* if, on out-of-sample picks at the
20- and 60-session horizons, the IN-cohort's mean excess is materially below the
OUT-cohort's AND removing the IN-cohort improves the source's edge vs its
dartboard baseline. Feature values come only from the snapshot taken at
recommendation time (`feature_version` v1) — no lookahead. The 5/10-session
readings are monitoring texture, never the verdict.

---

## H1 — "falling knife" · REGISTERED 2026-08-01

**Claim.** Source picks that are already in a short-term downtrend at pick time
underperform.

**Predicate (frozen v1 features, evaluated at recommendation time):**

```
ret_20d < 0  AND  px_over_sma50 < 0
```

(20-session return negative AND price below its 50-session average. Picks
missing either feature belong to neither cohort and are reported as `unknown`.)

**Provenance (in-sample, can never confirm H1).** Suggested by the 2026-07-18 →
07-20 cohorts (43 graded picks at the 5-session horizon on 2026-08-01): losers
averaged ret_20d −3.1% / px_over_sma50 −3.0% vs winners +4.0% / +3.6%. Known
confound: the losing cluster is dominated by one mid-July tech drawdown
(ORCL, CRM, QCOM, TXN, CRUS…), so the pattern may be a single market episode.
That is exactly why H1 must wait for out-of-sample picks.

**Verdict dates.** First out-of-sample 20-session readings: picks recommended
from 2026-08-03 onward mature from ~2026-08-31. 60-session verdict: from
~2026-10-27. (In-sample 20-session context lands ~2026-08-14 and is reported,
but labelled in-sample.)

**Status: REGISTERED — measuring.** Not a filter. Continuous readings:
`GET /v1/research/source-picks/autopsy` and the console Research page.

---

## H2 — "overheated entry" · REGISTERED 2026-08-30

**Claim.** Source picks whose price is already stretched well above its
50-session average at pick time underperform (the entry is late — the move has
happened).

**Predicate (frozen v1 features, evaluated at recommendation time):**

```
px_over_sma50 > 0.08
```

(Price more than 8% above its 50-session average. Picks missing the feature are
`unknown`. The threshold is pinned from the provenance read below and was NOT
tuned against any outcome — a different threshold is a different hypothesis.)

**Provenance (in-sample, can never confirm H2).** Two reads, both in-sample:

1. The 2026-08-30 counterfactual over the 49 graded desk BUY memos (a
   *different* corpus from `research.source_picks` — desk memos, not source
   picks — so it is context, not evidence for this table): vetoing
   `px_over_sma50 > 0.08` kept 41 calls at 44% hit / −1.73% avg vs SPY, against
   39% / −2.66% unfiltered — the least-bad of six vetoes tried, still negative,
   still below the dart. The worst July calls (AMD +11.9%, AMAT +15.4% and +12.4%
   above SMA50 at memo time) were bought on strength immediately before a sector
   drawdown.
2. The Principal's own reading of those losers ("bought overheated"), stated
   before this counterfactual was run.

Known confound: the same single July drawdown episode that motivated H1. H1's
own out-of-sample reading (knives *outperforming* non-knives at 5–10 sessions)
is a warning that one episode's pattern need not generalise — which is exactly
why H2 waits for out-of-sample picks like H1 did.

**Verdict dates.** Only picks recommended strictly after 2026-08-30 count.
First out-of-sample 20-session readings: picks from 2026-08-31 onward mature
from ~2026-09-28. 60-session verdict: from ~2026-11-24. In-sample context is
reported throughout, labelled in-sample.

**Status: REGISTERED — measuring.** Not a filter. Continuous readings:
`GET /v1/research/source-picks/autopsy` (hypothesis `H2-overheated-entry`) and
the console Research page. Code half of the registration:
`atlas/dcp/research/source_picks.py` (`HYPOTHESES` registry).

---

*Adding a hypothesis: append a section with a dated registration, an exact
predicate over frozen v1 features, provenance (what suggested it), and its
verdict dates; add the matching entry to the `HYPOTHESES` registry in
`atlas/dcp/research/source_picks.py`. Never edit a registered predicate —
retire and re-register.*
