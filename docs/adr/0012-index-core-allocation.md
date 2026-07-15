# ADR-0012 — Index-core allocation: be invested while factors prove out

Date: 2026-07-15 · Status: **Accepted** (signed by the Principal 2026-07-15) · Decider: Principal (Jay)

## Context
Atlas holds a hypothetical A$100k under a **capital-preservation-first**,
long-only mandate across US and India equities. Factor research is deliberately
slow (ADR-0011: one factor at a time through the gauntlet; most fail). Momentum
is the only validated active strategy today, approved for paper with recorded
concentration caveats (ADR-0010); PEAD is under test.

If capital sits in cash until a broad active book exists, the fund pays a large
opportunity cost — and for a capital-preservation mandate the evidence-backed
default when you hold no proven active edge is not cash, it is **owning the
market itself**. A passive index core *is* the benchmark every gate measures
against; it cannot "fail the gauntlet" because it is the gauntlet's yardstick.
ADR-0009 already reserved this as a Principal allocation decision. This ADR
makes it concrete and settles how it coexists with the invariants.

## The invariant problem this settles
A passive-core buy has no momentum or PEAD signal to cite, so it cannot flow
through the agent memo→proposal path (invariant 2: no BUY without DCP evidence).
Resolution: **the passive core is not an agent recommendation at all.** It is a
deterministic, Principal-parameterised target-weight policy. No committee, no
signal, no thesis — "hold the market" requires no agent judgment. The DCP
computes the rebalance trades to hit the target weights; they enter the queue
with origin `core_allocation`, authorised by the **Principal's signature itself**
(the directive is the evidence), and still pass the full risk engine
(broad-ETF concentration clears trivially). This preserves invariant 2 (no
agent number — in fact no agent), invariant 3 (risk checks still terminal), and
the human-seal rule.

## Decision
Split the book into a **passive core** and an **active satellite**.

**Recommended default weights (the Principal sets the final numbers):**
- **Core 70%** — passive index exposure, held, not stopped:
  - US **55%** via `SPY` (S&P 500 total-return proxy).
  - India **15%** via `INDA` (iShares MSCI India), the mandate's India sleeve.
- **Active satellite 30%** — the validated active strategies, equal-weighted
  across whatever has cleared the gauntlet: momentum today; PEAD added iff it
  passes; future survivors join pro-rata. Carries ADR-0006 stops.
- Residual cash from FX/settlement friction only; no deliberate cash sleeve
  unless a regime rule (ADR-0011, deferred) later sets one.

**Mechanics:**
- **Rebalance** quarterly, or whenever any core leg drifts more than **±5
  percentage points** from target, whichever comes first. Each rebalance is a
  Principal-signed `core_allocation` proposal set.
- The core is **rebalanced, not stopped** — you do not stop out of the market
  in a preservation core. This is a deliberate distinction from the active
  sleeve, which keeps its stops.
- FX to AUD via the existing daily rates; L11 non-base-currency exposure still
  applies to the aggregate book.
- The active satellite's sizing is scoped to the 30% envelope, so a momentum
  drawdown cannot consume core capital.

## Consequences
1. The fund is ~70% invested from day one at market returns, honestly, while
   the active sleeve earns its keep — the shape of a real core-satellite fund,
   not a stock-picker forced to be clever weekly.
2. New DCP work (small): a `core_allocation` proposal origin + a deterministic
   target-weight rebalancer + the console surface for it. No agent, prompt, or
   gauntlet change.
3. The active sleeve's demotion bands (ADR-0010) now protect a bounded 30%, so
   a suspended strategy reverts its share to core, not to cash.
4. Reporting/attribution must separate core (beta) from satellite (alpha) so
   the scorecard measures what the active strategies actually add over simply
   holding the index.

## Signed allocation (2026-07-15; amended 2026-07-16 by ADR-0014 option B)
The operating allocation is **core 70% (SPY 55% / INDA 15%), active satellite
20% (momentum 10% / PEAD 10%), cash 10%.** (The original 30% satellite / 0%
cash was amended to 20% / 10% when ADR-0014 option B was signed — a deliberate
cash reserve; core targets unchanged.) The mechanics are weight-agnostic.

Consequence for the in-flight book: the nine momentum proposals generated
2026-07-14 were sized to the FULL A$100k envelope (~A$69k total), which now
exceeds the 30% satellite cap. They are superseded by this ADR and must be
regenerated inside the satellite envelope; the active-sleeve sizing is held
until the PEAD verdict resolves how many validated factors share the 30%
(one factor → 30% momentum; two → 15%/15%). The passive core (70%) deploys
first and independently.
