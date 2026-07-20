# Atlas AI Capital — Institutional Review Package

Prepared for an independent, adversarial review by **GPT-5.6**, acting simultaneously as the
Investment Committee, Chief Quant Researcher, CTO, Principal Engineer, Chief Risk Officer, and
Head of Data Engineering of a top-tier quantitative hedge fund.

**This package exists to EXPOSE the system, not to justify it.** Weaknesses, technical debt,
assumptions, placeholders, and unbuilt features are stated explicitly. Where a claim is not
supported by code, the internal adversarial audit (below) was tasked with catching it.

## How this package was produced (disclosure)
- A shared **ground-truth file** (`00_GROUND_TRUTH.md`) was compiled from direct repo inspection
  (`wc`, `grep`, DB introspection, git) and instructs every author to *trust the code over intent*.
- Documents `02`–`15` were drafted by AI agents reading the actual source files, then each was
  **adversarially audited** by a separate agent whose only job was to catch overclaims and hidden
  weaknesses. Every audit returned "needs-fixes"; a **verification-then-fix pass** applied only the
  corrections that the code confirmed (some auditor items were themselves wrong and rejected).
- Documents `01`, `16`, `17`, `18`, `19`, `20` were written by the lead directly, for global
  consistency and maximal candor.
- **Caveat for the reviewer:** this is AI-assisted documentation of an AI-assisted codebase. Treat
  every quantitative claim as *to be verified against the cited file*. The internal review is not a
  substitute for yours — where your pass finds material issues these docs missed, that gap is itself
  a finding (and, per `17` Q21, the intended test of the internal-review methodology).

## Reading order
1. **`01_EXECUTIVE_SUMMARY.md`** — what it is, honestly; maturity; the 5 biggest limitations & risks.
2. **`16_KNOWN_LIMITATIONS.md`** — the brutal inventory (read this against §01's positives).
3. **`17_OPEN_QUESTIONS.md`** — decisions made but not validated.
4. **`18_REVIEW_CHECKLIST.md`** — your question bank, per reviewer hat.
5. Then the subsystem deep-dives (`02`–`15`) as your review dictates; `19` maps each to source; `20` is reference.

## Contents
| # | Document | Author |
|---|---|---|
| 00 | Ground Truth (shared facts baseline) | lead |
| 01 | Executive Summary | lead |
| 02 | System Architecture | agent + audit + fix |
| 03 | Data Pipeline | agent + audit + fix |
| 04 | Factor Library | agent + audit + fix |
| 05 | Scoring Engine | agent + audit + fix |
| 06 | Portfolio Construction | agent + audit + fix |
| 07 | Risk Management | agent + audit + fix |
| 08 | Backtesting | agent + audit + fix |
| 09 | AI Agent Design | agent + audit + fix |
| 10 | Codebase Overview | agent + audit + fix |
| 11 | Configuration Reference | agent + audit + fix |
| 12 | Dependencies | agent + audit + fix |
| 13 | Testing | agent + audit + fix |
| 14 | Security | agent + audit + fix |
| 15 | Performance | agent + audit + fix |
| 16 | **Known Limitations** (brutal) | lead |
| 17 | Open Questions | lead |
| 18 | Review Checklist for GPT-5.6 | lead |
| 19 | File Index (doc → source) | lead |
| 20 | Appendix (glossary, stats, diagrams) | lead |

## The three things to test hardest (the lead's view)
1. **Does the single validated strategy (12-1 momentum) survive out-of-sample and a regime
   change?** The +737% headline is a concentrated backtest, not expected return; the entire
   invested book (40% of NAV, ADR-0017) rests on it with no fallback sleeve.
2. **Is the operational posture acceptable even for paper?** Single machine, single process,
   **zero backups taken until the review night**, no proven restore, no API authentication.
3. **Is the process discipline real** (audit chain, no-agent-numbers wall, refuse-to-weaken-gates,
   honest graveyard of 8/9 lineages) **— or rigorous scaffolding around one fragile bet?**

## System status at time of review (2026-07-20)
Paper mode only · A$100k hypothetical · book was 100% cash (first fills pending) · 1 validated
strategy · 17 signed ADRs · ~1,515 tests passing · single-vendor data · single-machine deployment.
Nothing in this repository is investment advice.
