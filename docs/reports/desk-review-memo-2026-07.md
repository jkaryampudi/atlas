# Chair's Synthesis — Research Desk Review
**To:** Jay (Principal) · **From:** Panel Chair · **Date:** 2026-07-13
*Spot-verified before ranking: live_run.py:31 report pin + fallback text, runner.py cost line, grounding.py:59 substring test, desk.py:90 single debate client, xsmom-pit PASS report ("Gate verdict: PASS", 4/4 folds, null p 0.000, DSR 0.998), scorecard.py:227 is_active resolver vs analyze.py is_active=FALSE, and that `adjust_for_splits` is used by `backtest/real_run.py` but by none of build_evidence / scanner v1 / scorecard. All confirmed as the panel described.*

---

## 1. Where the panel agrees (3+ raised it)

1. **Evidence block 3 is a lie by staleness** (5 of 6; 3 named it their one thing). `build_evidence` pins a hardcoded FAIL report and tells every model "No validated strategy covers {symbol}" — while `docs/reports/xsmom-pit-sp500-2026-07.md` records a signed decision-grade PASS. Every nightly memo since 2026-07-10 argues from a quant record the fund itself has superseded, and it is the only evidence block scraped from a mutable markdown file instead of DCP tables.
2. **Per-role model routing is dead config** (LLM, DS, CE). `desk.py:90` builds one `debate_bull` client for all four debate calls; `ATLAS_MODEL_DEBATE_BEAR` can never fire; the local/3090 route has zero call sites. (Chair's correction: the CIO *does* get its own client at desk.py:93, so premium-CIO routing works today — the dead wiring is debate-side only.)
3. **The scorecard as launched cannot support any skill claim** (RH, DS, QI; CAT adds it writes corrupted rows). No base rate (always-REJECT grades well above coin-flip for free), conviction recorded but never scored or even defined, dissent never graded, and analyze-box memos on new symbols are permanently ungradeable (is_active mismatch + no forward bars). First 20-session outcomes mature imminently; grading rules must predate grades.
4. **Measure before you change anything** (LLM, DS, CE). No memo-quality eval harness exists — prompts are hashed and reviewed but never scored, the rebuttal round (~40% of spend) has unknown value, and any Haiku/local/Opus routing decision made today would be vibes. Shadow mode exists, tested, unused.

---

## 2. Ranked list (impact × urgency / effort)

| # | Item | Personas | Effort | When |
|---|------|----------|--------|------|
| 1 | **Rebuild evidence block 3 from `quant.trial_registry` + approval artifacts**: per-family verdicts verbatim (momentum-v1 FAIL *and* xsmom-pit PASS), per-symbol PIT-universe/winner-decile applicability, version-pinned ref, honest fallback retained. Delete the REPORT constant. Golden-pin. | RH, LLM, DS, CAT, QI | M | Before next nightly |
| 2 | **Split-adjust the three desk read paths** (build_evidence, scanner v1, scorecard both legs). The cage is structurally helpless here — grounded memos on false evidence, phantom −90% scanner ranks, and false outcomes written into an append-only table. `adjust_for_splits` is already property-tested; this is wiring + a 10:1 mid-window fixture. | CAT | M | Before the 12 memos' outcomes mature |
| 3 | **Per-model pricing table in runner.py:49** keyed on `result.model`, fail-closed on unknown models (bill highest known rate or refuse), local/\* = $0 with tokens recorded, rate-pair persisted per run. The $10 breaker is a constitutional control currently pricing every model at Sonnet rates. | LLM, CE | S | Today |
| 4 | **Grounding token-boundary fix**: `token not in corpus` by substring means small integers ground against "SMA20" and dates. Compare against `numeric_tokens(corpus)` as a set; add red-team tests for the bypass. Pure cage-strengthening. | LLM, RH | S | This week |
| 5 | **Scorecard honesty bundle**: dartboard base rates next to vindication (rate-minus-baseline), conviction + source slices, dissent-right as complement of `vindicated()`, operational conviction definition in the CIO prompt (words only), analysis-only instrument resolution + bounded bar top-up so external picks are gradeable. Measurement only, no self-tuning. | RH, DS, QI | S–M | Before first outcomes mature |
| 6 | **Desk-loop failure semantics**: bounded backoff on 429/5xx/timeouts only (never schema/grounding kills), per-symbol "transient" skips instead of aborting the shortlist, catch `BudgetExhausted` as holds not a crash, per-surface budget sub-caps (analyze can currently starve the nightly desk inside the shared $10), append violation text to the retry prompt as a reviewed template change. | LLM, CE | M | Before scaling nightly |
| 7 | **Un-dead debate routing + persist DebateCases**: per-side clients in `run_debate`; migration to store the four debate JSONs per memo (provenance pattern already exists). Prerequisite for measuring whether bull/bear are anchored copies — 80% of spend currently unmeasurable. | LLM, DS, CE | S–M | Next desk change window |
| 8 | **Memo-quality eval harness**: replay frozen persisted-evidence fixtures through the full cage; deterministic checks + pinned hashed LLM judge; prompt PRs attach eval deltas; rebuttal-ablation flag; shadow-mode gate for any Haiku/local promotion with a pre-registered ADR bar. | LLM, CE, DS | L | Before next prompt revision or model swap |
| 9 | **Earnings calendar**: vendor ingest, ISO-dates-only evidence block ("next earnings YYYY-MM-DD, N sessions after last bar"). ~1 in 3 memos straddles a print, and the scanner's heuristic is biased toward earnings-adjacent names without knowing it. Zero injection surface. | RH, CAT | M | Next 2–3 weeks |
| 10 | **Cheap evidence wiring, one PR**: SPY regime label from the existing classifier (closed vocab), scanner-context block (score components, "attention not prediction"), and move the QUESTION strings in desk.py/live_run.py into `prompts/` so the template hash covers them. All wiring, no new surface. | RH, CAT, QI | S | This sprint, rides with #1 |

Did not make the cut but logged: structured KillCriterion enum + nightly kill-monitor (RH, L effort — batch deliberately with the next schema revision); memo run-group cost attribution (CE); dividend/total-return scorecard tilt (CAT); peer/sector valuation table (RH, CAT — after #9); position-review vocabulary for held names (QI — needed before the first bridged position, not before).

---

## 3. The one thing this month

**Item 1: generate the quant-verdict block from the registry, before the next nightly cycle.** Five of six specialists converged on it independently, and the decision-scientist's framing is the sharpest: you cannot measure the judgment of a judge who is structurally permitted exactly one ruling. While block 3 asserts "no validated strategy exists," REJECT is the only reachable verdict, the 12/12 record is baked in rather than reasoned, conviction and dissent are uncalibratable, and every memo written tonight is a permanently audited judgment made on evidence the fund's own signed report contradicts. It also converts the desk's single unpinned, regex-scraped evidence source into what everything else already is — deterministic DCP output that cannot silently rot. The pricing fix (#3) is more urgent per dollar of effort — do it today — but it's an afternoon, not the month. Splits (#2) ships in the same push or immediately after; its deadline is the outcome-maturation clock.

---

## 4. Dissents worth preserving

- **CE's self-correction on caching**: evidence is *not* rebuilt per call (once per symbol, shared, persisted), and prompt caching would silently no-op below Sonnet's 2048-token cacheable-prefix minimum. The real cost lever is the Batch API, and the honest trigger is deferred: >10 symbols/night or ~$2/night recorded spend. Resist optimizing costs that don't exist yet.
- **CAT on vendor sentiment**: numeric sentiment scores would pass the cage (injection-safe through the `_number` choke point) and should stay out anyway — black-box short-horizon signal on a monthly book. "Passes the cage" is necessary, not sufficient. The ADR-0005 deferral stands.
- **QI's fork that needs a decision, not code**: when xsmom trades, do picks route through the desk (making the traded strategy "xsmom + unvalidated LLM overlay" — an unregistered trial by your own ADR-0002 discipline) or around it (desk as non-blocking FLAG-only idiosyncratic-risk screen, graded counterfactually)? QI argues around, and the chair agrees, but it must be decided before the first approved pick flows — retrofitting later means unattributable memos on the book.
- **RH's honesty boundary**: the numeric half of peer comparison is cage-compatible; qualitative moat analysis genuinely requires forbidden third-party text, and that exclusion is *correct* — do not fake it with vendor descriptions.
- **DS on restraint**: dissent and conviction get *measured* first (both are free — complements and joins over existing tables); nothing acts on either until measurement shows signal. Acting on an unvalidated signal is the same sin as trading an ungated strategy.

---

## 5. What the desk already does well

This is earned, and the panel's own record shows it: the two-plane cage held under a nine-scenario red-team suite; grounding kills happened in *live* runs and were fixed by changing evidence and prompts, never by weakening the verifier — the digit-free-fallback comment in live_run.py documenting the IBN kill is exactly the right instinct executed the right way. The 12/12 REJECT record was honest at the time it was written (no approved strategy existed, and the quant graveyard repeatedly vindicated it), failed runs' costs persist against the breaker, evidence corpora are persisted per memo so every judgment is replayable, and the scorecard was built to grade the desk before anyone asked it to flatter the desk. The panel found one class of defect almost everywhere it looked — evidence and measurement going stale around a cage that works — and no defect anywhere in the cage's design itself. That inversion is rare, and it is the reason every fix on this list is additive rather than remedial.