# TradingAgents vs Atlas — implementation comparison

Date: 2026-07-11 · Sources: arXiv 2412.20138 (v7, 3 Jun 2025, full text) and
TauricResearch/TradingAgents `main` (code-level review; the repo has evolved past
the paper — structured outputs, SQLite checkpointing, file-based memory, look-ahead
data guards). Compared against Atlas at commit `df5ca77`.

## TL;DR

Their strength is **deliberation structure**; Atlas's is **governance where
deliberation meets money**. Notably, their own repo never executes a trade — the
terminal artifact is an advisory 5-tier rating string. The four patterns worth
having (debate, grounding, checkpoints, per-role models) were adopted under
governance in ADR-0005; everything we rejected (LLM sizing, agent-held approval,
risk-as-LLM-debate) is confirmed present-or-absent in their design exactly as the
ADR anticipated.

| Dimension | TradingAgents | Atlas |
|---|---|---|
| Debate | Bull/bear free-prose, n rounds, facilitator **picks a winner** | 4 schema-validated calls, forced genuine concession, stance integrity enforced; **advisory only** — cannot open a BUY |
| Final decision | LLM portfolio manager (can override LLM risk team); no human | CIO recommends only; Risk Engine (code) approves risk; human approves trades |
| Sizing/prices | Trader emits `entry_price`/`stop_loss` floats + free-text "5% of portfolio"; **nothing validates or bounds them** | Agents may not produce execution numbers at all (schema-enforced); size = deterministic §4 formula, property-tested against every cap |
| Risk | Three LLM personas debate 1 round; **zero code-level limits anywhere** | L1–L11 in code, itemised verdicts, worst-case pro-forma, FAIL terminal, DD breakers latch until dual-confirmed human action |
| Grounding | Verified market-data snapshot as *input*; **no post-hoc check of LLM claims**; schema fallback to unvalidated free text | Post-hoc verbatim check: every numeric claim must exist in the *cited* evidence; fail-closed with audit events |
| Injection defense | **Absent** — Reddit/X/news interpolated raw into prompts | Untrusted-content fencing + red-team suite; sentiment analyst deferred until the injection corpus covers social media (ADR-0005) |
| Checkpointing | LangGraph SqliteSaver, thread = ticker+date+config, cleared on success | `workflow` schema tables, resume skips done nodes, every node completion is a hash-chained audit event |
| Memory/learning | Markdown memory file + deferred reflection on realized returns (repo; paper defines none) | `learning` schema: outcome labels, counterfactual ledger, Brier-scored calibration under Tier 1–3 bounds (agent-level reflection loop lands Phase 5) |
| Models | Deep-think vs quick-think split only (**no per-role config**); Ollama/OpenAI-compat local support | Per-role `ATLAS_MODEL_<ROLE>` registry + `local/` OpenAI-compat route + shadow_mode for upgrades (Constitution 7.2) |
| Backtest rigor | 3 months, one bull quarter, no transaction costs, no trial counting; SR>8 self-flagged as beyond expected range | Trial registry with true-count deflated Sharpe, 1000-path null-model gate, purged walk-forward, overfit canary — and momentum v1 honestly **failed** these gates on real data |
| Audit | Per-run state JSON logs | Append-only hash chain, tamper detection, nightly verification job |

## Where they are genuinely ahead

1. **Analyst breadth and data flows.** Four concurrent analyst roles over
   yfinance/Alpha Vantage/FRED/Reddit/Stocktwits/Polymarket with staleness guards
   and tested news look-ahead windows. Atlas has one vendor (EODHD) and no
   sentiment plane (deferred deliberately, but still absent).
2. **Reflection is live.** Their memory writes realized raw/alpha returns back as
   lessons on the next run for a ticker. Atlas's learning loop is schema-complete
   but the closed loop is a Phase 5 exit criterion, not running code.
3. **Provider breadth.** OpenAI/Anthropic/Google/Bedrock/Azure client factory with
   per-provider reasoning knobs; Atlas has Anthropic + one OpenAI-compatible route.
4. **Verified-input snapshot.** Their `market_data_validator` injects a verified
   OHLCV/indicator table as source-of-truth *before* generation — complementary to
   our post-hoc grounding check, and worth considering as an addition (belt and
   braces on both sides of the call).

## Where the architectures fundamentally diverge

Their pipeline is **LLM all the way down**: the risk debate is rhetoric between
three personas, the portfolio manager that overrides it is another LLM, schema
validation silently falls back to free text after one failure, and no number an
agent emits is ever bounded by code. That is a defensible research design — and
the repo is framed research-only — but it is exactly the architecture Doc 04 §1
exists to prevent: risk as opinion rather than structural property. The paper's
own evaluation illustrates why the discipline matters: a single 3-month bull
quarter, no costs, and a Sharpe above 8 that the authors themselves flag as
outside the plausible range — the precise failure mode Atlas's trial registry,
null-model gate, and deflated Sharpe are built to catch. When Atlas ran its first
real-data validation, the gates said FAIL, and that verdict shipped.

## Follow-ups worth proposing (not commitments)

- Verified-input snapshot pattern for DCP evidence blocks (complement to grounding).
- Sentiment/news analyst once the injection corpus covers social content (ADR-0005 deferral).
- Multi-vendor adapter breadth (second EOD source would also harden the gates).
